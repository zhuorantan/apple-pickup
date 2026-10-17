import aiohttp
import asyncio
import logging
import random
from telegram.ext import ApplicationBuilder, ExtBot
from telegram.helpers import escape_markdown

POLL_INTERVAL = 30
MAX_RETRY_DELAY = 15 * 60


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


async def __get_status(session: aiohttp.ClientSession, models: set[str], country_code: str, location: str):
  country_code = (country_code or '').strip('/').lower()
  base_url = 'https://www.apple.com'
  if country_code == 'cn':
    base_url = 'https://www.apple.com.cn'
  elif country_code and country_code != 'us':
    base_url += f'/{country_code}'
  url = f'{base_url}/shop/retail/pickup-message'
  params = {'pl': 'true', 'mts.0': 'regular', 'location': location}
  params.update({f'parts.{i}': model for i, model in enumerate(sorted(models))})
  logging.info('Fetching pickup availability for %s near %s', ', '.join(sorted(models)), location)

  async with session.get(url, params=params) as response:
    response.raise_for_status()
    return _parse_status(await response.json(), models)


async def __start(bot: ExtBot, chat_ids: set[int], models: set[str], country_code: str, location: str):
  if not models or not location:
    raise ValueError('At least one model and a location are required')
  status_by_model = {}
  retry_delay = POLL_INTERVAL

  async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
    while True:
      try:
        status = await __get_status(session, models, country_code, location)
      except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)
        if isinstance(error, aiohttp.ClientResponseError) and error.status in (403, 429, 541):
          retry_delay = MAX_RETRY_DELAY
        logging.warning('Pickup check failed; availability unknown, keeping last confirmed results. '
                        'Retrying in %s seconds: %s', retry_delay, error)
        await asyncio.sleep(retry_delay)
        continue

      retry_delay = POLL_INTERVAL
      for model, (product, stores, pending) in status.items():
        current_status = (stores, pending)
        if status_by_model.get(model) == current_status:
          continue
        status_by_model[model] = current_status
        logging.info('Pickup status changed for %s: available=%s, pending=%s', product, stores, pending)

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
            logging.error('Failed to send message to chat %s: %s', chat_id, error)

      await asyncio.sleep(POLL_INTERVAL * random.uniform(0.8, 1.2))


def run(token: str, chat_ids: set[int], models: set[str], country_code, location: str):
  app_builder = ApplicationBuilder().token(token)
  app = app_builder.build()
  asyncio.run(__start(app.bot, chat_ids, models, country_code, location))
