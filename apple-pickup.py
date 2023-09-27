import argparse
import logging
import os
from bot import run

logging.basicConfig(
  format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
  level=logging.INFO,
)

if __name__ == "__main__":
  def get_chat_ids_from_env():
    chat_ids = []

    while True:
      chat_id = os.environ.get('APPLE_PICKUP_CHAT_ID_' + str(len(chat_ids)))
      if chat_id is None:
        break
      chat_ids.append(int(chat_id))

    if 'APPLE_PICKUP_CHAT_ID' in os.environ:
      chat_ids.append(int(os.environ['APPLE_PICKUP_CHAT_ID']))

    return chat_ids

  def get_models_from_env():
    models = []

    while True:
      model = os.environ.get('APPLE_PICKUP_MODEL_' + str(len(models)))
      if model is None:
        break
      models.append(model)

    if 'APPLE_PICKUP_MODEL' in os.environ:
      models.append(os.environ['APPLE_PICKUP_MODEL'])

    return models

  parser = argparse.ArgumentParser()
  parser.add_argument(
    '--telegram-token',
    type=str,
    default=os.environ.get('APPLE_PICKUP_TELEGRAM_TOKEN'),
    required='APPLE_PICKUP_TELEGRAM_TOKEN' not in os.environ,
    help="Telegram bot token. Get it from https://t.me/BotFather.",
  )

  parser.add_argument(
    '--chat-id',
    action='append',
    type=int,
    default=get_chat_ids_from_env(),
    help= "IDs of Allowed chats. Can be specified multiple times. If not specified, the bot will respond to all chats.",
  )
  parser.add_argument(
    '--model',
    action='append',
    type=str,
    default=get_models_from_env(),
    help="Apple product model.",
  )
  parser.add_argument(
    '--country-code',
    type=str,
    default=os.environ.get('APPLE_PICKUP_COUNTRY_CODE'),
    help="Apple store country code.",
  )
  parser.add_argument(
    '--location',
    type=str,
    default=os.environ.get('APPLE_PICKUP_LOCATION'),
    help="Apple store location.",
  )

  parser.add_argument(
    '--data-dir',
    type=str,
    default=os.environ.get('APPLE_PICKUP_DATA_DIR'),
    help="Directory to store data. If not specified, data won't be persisted.",
  )
  parser.add_argument(
    '--webhook-url',
    type=str,
    default=os.environ.get('APPLE_PICKUP_WEBHOOK_URL'),
    help="URL for telegram webhook requests. If not specified, the bot will use polling mode.",
  )
  parser.add_argument(
    '--webhook-listen-address',
    type=str,
    default=os.environ.get('APPLE_PICKUP_WEBHOOK_LISTEN_ADDRESS') or '0.0.0.0:80',
    help="Address to listen for telegram webhook requests in the format of <ip>:<port>. Only valid when --webhook-url is set. If not specified, 0.0.0.0:80 would be used.",
  )
  
  args = parser.parse_args()
  run(args.telegram_token, set(args.chat_id), set(args.model), args.country_code, args.location)
