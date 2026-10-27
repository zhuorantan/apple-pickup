"""Regression for timed-out DNS work leaking a shielded-future exception."""
import asyncio
import socket
import time
import unittest

import aiohttp
from aiohttp.abc import AbstractResolver


class SlowFailingResolver(AbstractResolver):
  async def resolve(self, host, port=0, family=socket.AF_INET):
    def fail():
      time.sleep(.08)
      raise socket.gaierror(socket.EAI_AGAIN, 'simulated delayed DNS failure')
    return await asyncio.get_running_loop().run_in_executor(None, fail)

  async def close(self):
    pass


class DNSCancellationTests(unittest.IsolatedAsyncioTestCase):
  async def test_uncached_dns_does_not_leave_an_unhandled_future(self):
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    try:
      async with aiohttp.ClientSession(
          connector=aiohttp.TCPConnector(force_close=True, use_dns_cache=False,
                                         resolver=SlowFailingResolver()),
          timeout=aiohttp.ClientTimeout(total=.02)) as session:
        with self.assertRaises(asyncio.TimeoutError):
          await session.get('https://example.invalid')
        await asyncio.sleep(.15)
      self.assertEqual(errors, [])
    finally:
      loop.set_exception_handler(previous)


if __name__ == '__main__':
  unittest.main()
