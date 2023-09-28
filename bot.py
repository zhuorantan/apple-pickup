import aiohttp
import asyncio
import logging
from telegram.ext import ApplicationBuilder, ExtBot

async def __get_status(session: aiohttp.ClientSession, models: set[str], country_code: str, location: str):
  status = {}

  for model in models:
    url = f'https://www.apple.com/{country_code}/shop/fulfillment-messages?pl=true&parts.0={model}&location={location}'
    logging.info(f'Fetching {url}')
    async with session.get(url) as response:
      data = await response.json()

      stores_data = data['body']['content']['pickupMessage']['stores']
      if not stores_data:
        logging.warning(f'No stores found for {model}. data: {data}')
        continue

      productName = None
      stores = []

      for store in stores_data:
        storeName = store['storeName']
        part = store['partsAvailability'].get(model)
        if not part:
          continue
        productName = part['messageTypes']['regular']['storePickupProductTitle']
        available = part['messageTypes']['regular']['storeSelectionEnabled']
        if available:
          stores.append(storeName)

      status[model] = (productName, stores)

  return status

async def __start(bot: ExtBot, chat_ids: set[int], models: set[str], country_code: str, location: str):
  session = aiohttp.ClientSession()
  last_status_by_model = {}

  while True:
    status_by_model = await __get_status(session, models, country_code, location)

    for product, status in status_by_model.items():
      if last_status_by_model.get(product) == status:
        continue

      product, stores = status

      for chat_id in chat_ids:
        if not stores:
          message = f'❌ *{product}* is not available for pickup'
        else:
          store_list = '\n'.join(f'• {store}' for store in stores)
          message = f'✅ *{product}* is available for pickup at:\n{store_list}'

        await bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')

    last_status_by_model = status_by_model

    await asyncio.sleep(20)

def run(token: str, chat_ids: set[int], models: set[str], country_code, location: str):
  app_builder = ApplicationBuilder().token(token)
  app = app_builder.build()
  asyncio.run(__start(app.bot, chat_ids, models, country_code, location))
