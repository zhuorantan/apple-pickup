import asyncio
import logging
from pathlib import Path
import runpy
import httpx
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import bot


def payload():
  stores = []
  for name in ('Available Store', 'Other Store'):
    parts = {}
    for model, title, state in (
        ('A', 'Available\xa0Product', 'available' if name == 'Available Store' else 'unavailable'),
        ('U', 'Unavailable Product', 'unavailable'),
        ('P', 'Pending Product', 'default')):
      parts[model] = {'pickupDisplay': state, 'messageTypes': {'regular': {
        'storePickupProductTitle': title, 'storePickupQuote': 'Check back later',
        'storeSelectionEnabled': False}}}
    stores.append({'storeName': name, 'partsAvailability': parts})
  return {'head': {'status': 200}, 'body': {'stores': stores}}


def session(data):
  response = MagicMock()
  response.json = AsyncMock(return_value=data)
  context = MagicMock()
  context.__aenter__ = AsyncMock(return_value=response)
  return SimpleNamespace(get=MagicMock(return_value=context))


class LoggingTests(unittest.IsolatedAsyncioTestCase):
  async def test_compact_results_repeat_without_repeating_notifications(self):
    http = session(payload())
    telegram = SimpleNamespace(send_message=AsyncMock())
    real_get = getattr(bot, '__get_status')
    async def get_status(unused, models, country, location, **kwargs):
      return await real_get(http, models, country, location)
    with self.assertLogs(level='INFO') as logs, \
        patch.object(bot.VPNPool, 'from_env', return_value=None), \
        patch.object(bot, '__get_status', side_effect=get_status), \
        patch.object(bot.asyncio, 'sleep', AsyncMock(side_effect=[None, asyncio.CancelledError()])):
      with self.assertRaises(asyncio.CancelledError):
        await getattr(bot, '__start')(telegram, {1}, {'A', 'U', 'P'}, 'ca', 'Location')
    results = [line for line in logs.output if 'Pickup check succeeded:' in line]
    self.assertEqual(len(results), 2)
    for line in results:
      self.assertIn('models=3 stores=2; unavailable=1; pending=1;', line)
      self.assertIn("available='Available Product' at ['Available Store']", line)
      for omitted in ('Other Store', 'Unavailable Product', 'Pending Product', 'Check back later'):
        self.assertNotIn(omitted, line)
    self.assertFalse(any('Pickup status changed' in line for line in logs.output))
    self.assertEqual(telegram.send_message.await_count, 3)

  async def test_all_unavailable_are_one_count_without_model_or_location_list(self):
    data = payload()
    for store in data['body']['stores']:
      for part in store['partsAvailability'].values():
        part['pickupDisplay'] = 'unavailable'
    with self.assertLogs(level='INFO') as logs:
      await getattr(bot, '__get_status')(session(data), {'A', 'U', 'P'}, 'ca', 'Location')
    self.assertEqual(len(logs.output), 1)
    self.assertIn('unavailable=3; available=none', logs.output[0])
    self.assertNotIn('Product', logs.output[0])
    self.assertNotIn('Store', logs.output[0])

  async def test_incomplete_response_logs_no_confirmed_results(self):
    data = payload()
    del data['body']['stores'][-1]['partsAvailability']['U']['messageTypes']
    with self.assertLogs(level='DEBUG') as logs:
      with self.assertRaises(ValueError):
        await getattr(bot, '__get_status')(session(data), {'A', 'U', 'P'}, 'ca', 'Location')
    self.assertFalse(any('Pickup check succeeded' in line for line in logs.output))

  async def test_product_and_location_newlines_stay_on_one_log_line(self):
    data = payload()
    data['body']['stores'][0]['storeName'] = 'Available\nStore'
    for store in data['body']['stores']:
      store['partsAvailability']['A']['messageTypes']['regular']['storePickupProductTitle'] = 'Available\nProduct'
    with self.assertLogs(level='INFO') as logs:
      await getattr(bot, '__get_status')(session(data), {'A', 'U', 'P'}, 'ca', 'Location')
    self.assertIn("'Available\\nProduct' at ['Available\\nStore']", logs.output[0])
    self.assertTrue(all('\n' not in line for line in logs.output))

  async def test_telegram_failure_logs_no_chat_id_or_exception_secrets(self):
    sensitive = 'private-token-in-request-error'
    telegram = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError(sensitive)))
    with self.assertLogs(level='INFO') as logs, \
        patch.object(bot.VPNPool, 'from_env', return_value=None), \
        patch.object(bot, '__get_status', AsyncMock(return_value={'A': ('Product', [], [])})), \
        patch.object(bot.asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError())):
      with self.assertRaises(asyncio.CancelledError):
        await getattr(bot, '__start')(telegram, {987654321}, {'A'}, 'ca', 'Location')
    output = '\n'.join(logs.output)
    self.assertIn('Failed to send pickup notification (RuntimeError)', output)
    self.assertNotIn(sensitive, output)
    self.assertNotIn('987654321', output)

  async def test_http_client_does_not_log_telegram_token_urls(self):
    levels = {name: logging.getLogger(name).level for name in ('httpx', 'httpcore')}
    try:
      runpy.run_path(str(Path(bot.__file__).with_name('apple-pickup.py')))
      with self.assertLogs(level='INFO') as logs:
        logging.info('Checking HTTP logging')
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'ok': True}))
        async with httpx.AsyncClient(transport=transport) as client:
          await client.get('https://api.telegram.org/botprivate-test-token/getMe')
      self.assertNotIn('private-test-token', '\n'.join(logs.output))
      self.assertNotIn('HTTP Request:', '\n'.join(logs.output))
    finally:
      for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


if __name__ == '__main__':
  unittest.main()
