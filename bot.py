import aiohttp
import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from telegram.ext import ApplicationBuilder, ExtBot
from telegram.helpers import escape_markdown
from contextlib import nullcontext
from vpn import VPNPool

POLL_INTERVAL = 10  # With 20% jitter, wait 8-12 seconds after each successful check.
RETRY_BASE_DELAY = 15
MAX_RETRY_DELAY = 15 * 60
BLOCK_COOLDOWNS = (5 * 60, 10 * 60, 20 * 60, 30 * 60)


def _retry_after_seconds(value):
  if not value:
    return None
  value = value.strip()
  try:
    if value.isascii() and value.isdigit():
      return int(value)
    retry_at = parsedate_to_datetime(value)
    if retry_at.tzinfo is None:
      retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
  except (TypeError, ValueError, OverflowError):
    return None


def _parse_status(data, models: set[str]):
  status = {}
  try:
    if str(data['head']['status']) != '200' or data['body'].get('errorMessage'):
      raise ValueError('Apple returned an unsuccessful pickup response')
    stores_data = data['body']['stores']
    if not isinstance(stores_data, list) or not stores_data:
      raise ValueError('Apple returned no stores; check the location and model numbers')

    for store in stores_data:
      store_name = store['storeName']
      if not isinstance(store_name, str) or not store_name:
        raise ValueError('Apple returned an invalid store name')
      for model in models:
        # Incomplete results are unknown, not evidence that a store lost stock.
        part = store['partsAvailability'][model]
        pickup_display = part['pickupDisplay']
        if pickup_display not in ('available', 'unavailable', 'ineligible', 'default'):
          raise ValueError(f'Unknown pickup status for {model}: {pickup_display!r}')
        regular = part['messageTypes']['regular']
        product_name = regular['storePickupProductTitle']
        if not isinstance(product_name, str) or not product_name:
          raise ValueError(f'Apple returned an invalid product name for {model}')
        if model not in status:
          status[model] = (product_name, [], [])
        if pickup_display == 'available':
          status[model][1].append(store_name)
        elif pickup_display == 'default':
          quote = regular.get('storePickupQuote')
          if regular.get('storeSelectionEnabled') is not False or not isinstance(quote, str) or not quote.strip():
            raise ValueError(f'Ambiguous default pickup status for {model}')
          # Apple may defer pickup availability (for example until launch day).
          # Preserve its message instead of treating this as confirmed no stock.
          status[model][2].append((store_name, quote.strip()))
  except (KeyError, TypeError, AttributeError) as error:
    raise ValueError('Apple returned an incomplete or unexpected pickup response') from error

  return {
    model: (product, sorted(set(stores)), sorted(set(pending)))
    for model, (product, stores, pending) in status.items()
  }


def _store_base_url(country_code: str):
  country_code = (country_code or '').strip('/').lower()
  base_url = 'https://www.apple.com'
  if country_code == 'cn':
    base_url = 'https://www.apple.com.cn'
  elif country_code and country_code != 'us':
    base_url += f'/{country_code}'
  return base_url


async def __get_status(session: aiohttp.ClientSession, models: set[str], country_code: str, location: str, *, proxy=None, route_name="direct"):
  url = f'{_store_base_url(country_code)}/shop/retail/pickup-message'
  params = {'pl': 'true', 'mts.0': 'regular', 'location': location}
  params.update({f'parts.{i}': model for i, model in enumerate(sorted(models))})
  logging.debug('Fetching pickup availability for %s near %s', ', '.join(sorted(models)), location)

  started = asyncio.get_running_loop().time()
  async with session.get(url, params=params, proxy=proxy) as response:
    response.raise_for_status()
    data = await response.json()
    status = _parse_status(data, models)
    available = []
    unavailable = 0
    pending_count = 0
    for model in sorted(status):
      product, stores, pending = status[model]
      if stores:
        product = product.replace('\xa0', ' ')
        available.append(f"{product!r} at {stores!r}")
      elif pending:
        pending_count += 1
      else:
        unavailable += 1
    # Keep routine results compact; locations matter only when pickup is available.
    summary = [f"unavailable={unavailable}"]
    if pending_count:
      summary.append(f"pending={pending_count}")
    summary.append("available=" + (' | '.join(available) if available else 'none'))
    logging.info('Pickup check succeeded: route=%s duration=%.2fs models=%s stores=%s; %s',
                 route_name, asyncio.get_running_loop().time() - started,
                 len(status), len(data['body']['stores']), '; '.join(summary))
    return status


async def __start(bot: ExtBot, chat_ids: set[int], models: set[str], country_code: str, location: str):
  if not models or not location:
    raise ValueError('At least one model and a location are required')
  status_by_model = {}
  retry_delay = RETRY_BASE_DELAY
  block_level = 0
  vpn = VPNPool.from_env(hostname=urlsplit(_store_base_url(country_code)).hostname)
  logging.info('Pickup polling interval: %s seconds with 20%% jitter; VPN failover %s',
               POLL_INTERVAL, 'enabled' if vpn else 'disabled')

  # Keep connections reusable without accepting or replaying Apple's session cookies.
  async with aiohttp.ClientSession(
      timeout=aiohttp.ClientTimeout(total=10), cookie_jar=aiohttp.DummyCookieJar(),
      # A new connection for each VPN request avoids stale sockets after reconnects.
      # Avoid shielded DNS tasks surviving request cancellation during VPN recovery.
      # Gluetun already caches DNS responses in VPN mode.
      connector=aiohttp.TCPConnector(force_close=vpn is not None,
                                     use_dns_cache=vpn is None)) as session:
    async with vpn.open(session) if vpn else nullcontext():
      while True:
        route = await vpn.route() if vpn else None
        try:
          status = await __get_status(session, models, country_code, location,
                                      proxy=route.proxy_url if route else None,
                                      route_name=route.name if route else "direct")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
          vpn_block = isinstance(error, aiohttp.ClientResponseError) and error.status == 541 and vpn is not None
          transport_failure = vpn is not None and (
            isinstance(error, (asyncio.TimeoutError, aiohttp.ClientConnectionError,
                               aiohttp.ClientPayloadError, aiohttp.ClientHttpProxyError)))
          if vpn_block or transport_failure:
            logging.warning('Pickup check failed on %s (%s); keeping last confirmed results',
                            route.name, type(error).__name__)
            vpn.fail(session, route, blocked=vpn_block)
            retry_delay = RETRY_BASE_DELAY
            continue
          if isinstance(error, aiohttp.ClientResponseError) and error.status in (403, 541):
            delay = BLOCK_COOLDOWNS[block_level]
            block_level = min(block_level + 1, len(BLOCK_COOLDOWNS) - 1)
          else:
            delay = retry_delay
            retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
            if isinstance(error, aiohttp.ClientResponseError) and error.status == 429:
              retry_after = _retry_after_seconds((error.headers or {}).get('Retry-After'))
              if retry_after is not None:
                # Do not retry before Apple's deadline or spin on an expired one.
                delay = max(RETRY_BASE_DELAY, retry_after)
          logging.warning('Pickup check failed; availability unknown, keeping last confirmed results. '
                          'Retrying in %s seconds: %s: %s', delay, type(error).__name__,
                          str(error) or 'request timed out')
          await asyncio.sleep(delay)
          continue

        retry_delay = RETRY_BASE_DELAY
        block_level = 0
        for model, (product, stores, pending) in status.items():
          current_status = (stores, pending)
          if status_by_model.get(model) == current_status:
            continue
          status_by_model[model] = current_status
          logging.debug('Pickup status changed for %s: available=%s, pending=%s', product, stores, pending)

          product = escape_markdown(product, version=2)
          if stores:
            store_list = '\n'.join(f'• {escape_markdown(store, version=2)}' for store in stores)
            message = f'✅ *{product}* is available for pickup at:\n{store_list}'
          elif pending:
            message = f'ℹ️ Pickup status for *{product}*:'
          else:
            message = f'❌ *{product}* is not available for pickup'
          if pending:
            updates = '\n'.join(
              f'• {escape_markdown(store, version=2)}: {escape_markdown(quote, version=2)}'
              for store, quote in pending
            )
            message += f'\n{updates}'

          for chat_id in chat_ids:
            try:
              await bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')
            except Exception as error:
              logging.error('Failed to send pickup notification (%s)', type(error).__name__)

        await asyncio.sleep(POLL_INTERVAL * random.uniform(0.8, 1.2))


def run(token: str, chat_ids: set[int], models: set[str], country_code, location: str):
  app_builder = ApplicationBuilder().token(token)
  app = app_builder.build()
  asyncio.run(__start(app.bot, chat_ids, models, country_code, location))
