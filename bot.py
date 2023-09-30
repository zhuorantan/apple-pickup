import aiohttp
import asyncio
import logging
from telegram.ext import ApplicationBuilder, ExtBot

async def __get_status(session: aiohttp.ClientSession, models: set[str], country_code: str, location: str):
  status = {}

  parts = '&'.join(f'parts.{i}={model}' for i, model in enumerate(models))
  url = f'https://www.apple.com/{country_code}/shop/fulfillment-messages?pl=true&{parts}&location={location}'
  logging.info(f'Fetching {url}')

  async with session.get(url) as response:
    data = await response.json()
    stores_data = data['body']['content']['pickupMessage']['stores']

    for store in stores_data:
      for model in models:
        part = store['partsAvailability'].get(model)
        storeName = store['storeName']
        if not part:
          logging.warning(f'No data for model {model} in store {storeName}')
          continue

        productName = part['messageTypes']['regular']['storePickupProductTitle']
        available = part['messageTypes']['regular']['storeSelectionEnabled']
        if model not in status:
          status[model] = (productName, [])

        if not available:
          continue

        status[model][1].append(storeName)

  return status

async def __start(bot: ExtBot, chat_ids: set[int], models: set[str], country_code: str, location: str):
  session = aiohttp.ClientSession()
  stores_by_model = {}

  while True:
    for model, (product, stores) in (await __get_status(session, models, country_code, location)).items():
      last_stores = stores_by_model.get(model, [])
      if last_stores == stores:
        continue
      stores_by_model[model] = stores

      logging.info(f'Product availability changed for {product}, from {last_stores} to {stores}')

      for chat_id in chat_ids:
        if not stores:
          message = f'❌ *{product}* is not available for pickup'
        else:
          store_list = '\n'.join(f'• {store}' for store in stores)
          message = f'✅ *{product}* is available for pickup at:\n{store_list}'

        try:
          await bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')
        except Exception as e:
          logging.error(f'Failed to send message to chat {chat_id}: {e}')

    await asyncio.sleep(15)

def run(token: str, chat_ids: set[int], models: set[str], country_code, location: str):
  app_builder = ApplicationBuilder().token(token)
  app = app_builder.build()
  asyncio.run(__start(app.bot, chat_ids, models, country_code, location))
