import aiohttp
import asyncio
from telegram.ext import ApplicationBuilder, ExtBot

async def __get_status(session: aiohttp.ClientSession, models: set[str], country_code: str, location: str):
  status = []

  for model in models:
    url = f'https://www.apple.com/{country_code}/shop/fulfillment-messages?pl=true&parts.0={model}&location={location}'
    async with session.get(url) as response:
      data = await response.json()

      productName = None
      stores = []

      for store in data['body']['content']['pickupMessage']['stores']:
        storeName = store['storeName']
        part = store['partsAvailability'].get(model)
        if not part:
          continue
        productName = part['messageTypes']['regular']['storePickupProductTitle']
        available = part['messageTypes']['regular']['storeSelectionEnabled']
        if available:
          stores.append(storeName)

      status.append((productName, stores))

  return status

async def __start(bot: ExtBot, chat_ids: set[int], models: set[str], country_code: str, location: str):
  session = aiohttp.ClientSession()
  last_status = None

  while True:
    status = await __get_status(session, models, country_code, location)

    if status != last_status:
      last_status = status
      for chat_id in chat_ids:
        for product, stores in status:
          if not stores:
            message = f'❌ *{product}* is not available for pickup'
          else:
            store_list = '\n'.join(f'• {store}' for store in stores)
            message = f'✅ *{product}* is available for pickup at:\n{store_list}'

          await bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')

    await asyncio.sleep(20)

def run(token: str, chat_ids: set[int], models: set[str], country_code, location: str):
  app_builder = ApplicationBuilder().token(token)
  app = app_builder.build()
  asyncio.run(__start(app.bot, chat_ids, models, country_code, location))
