import asyncio
from contextlib import asynccontextmanager
import logging
import os
import time
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

import aiohttp
import bot
from vpn import VPNPool

CONFIG=[dict(name=n,control_url=f'http://{n}:8000',proxy_url=f'http://{n}:8888') for n in ('a','b')]

class PoolTests(unittest.IsolatedAsyncioTestCase):
  def pool(self):
    return VPNPool(CONFIG, 'test-key')

  async def test_switch_does_not_wait_for_background_reconnect(self):
    pool=self.pool()
    a,b=pool.endpoints
    a.ready=b.ready=True
    a.public_ip='1.1.1.1'; b.public_ip='2.2.2.2'
    pool.active=a
    started=asyncio.Event(); release=asyncio.Event()
    async def repair(*args):
      started.set()
      await release.wait()
    pool._prepare=repair
    async with pool.open(None):
      # open starts initial preparations; restore the ready state for this scenario.
      a.ready=b.ready=True
      pool.fail(None,a,blocked=True)
      self.assertIs(await asyncio.wait_for(pool.route(),.1),b)
      self.assertIn(a.public_ip,pool.blocked_ips)
      await started.wait()
      self.assertFalse(a.task.done())
      self.assertIs(await pool.route(),b)
    self.assertTrue(a.task.cancelled())

  async def test_no_ready_route_waits_without_direct_fallback(self):
    pool=self.pool()
    waiter=asyncio.create_task(pool.route())
    await asyncio.sleep(.01)
    self.assertFalse(waiter.done())
    pool.endpoints[1].ready=True
    pool.changed.set()
    self.assertIs(await asyncio.wait_for(waiter,.1),pool.endpoints[1])

  async def test_repair_restarts_vpn_then_dns_and_publishes(self):
    pool=self.pool(); a=pool.endpoints[0]
    pool._restart=AsyncMock()
    pool._wait_for_route=AsyncMock(return_value='1.1.1.1')
    await pool._prepare(None,a,True)
    self.assertEqual([c.args[-1] for c in pool._restart.await_args_list],['vpn','dns'])
    self.assertTrue(a.ready)
    self.assertTrue(pool.changed.is_set())

  async def test_duplicate_and_blocked_ips_are_rejected(self):
    for blocked in (True,False):
      pool=self.pool(); a,b=pool.endpoints
      if blocked:
        pool.blocked_ips['1.1.1.1']=time.monotonic()+60
      else:
        b.ready=True; b.public_ip='1.1.1.1'
      pool._wait_for_route=AsyncMock(return_value='1.1.1.1')
      with patch('vpn.asyncio.sleep',side_effect=asyncio.CancelledError):
        with self.assertRaises(asyncio.CancelledError):
          await pool._prepare(None,a,False)
      self.assertFalse(a.ready)

  async def test_failed_background_repair_is_retried_and_not_ready(self):
    pool=self.pool(); a=pool.endpoints[0]
    pool._wait_for_route=AsyncMock(side_effect=RuntimeError('unexpected'))
    with patch('vpn.asyncio.sleep',side_effect=asyncio.CancelledError) as sleep:
      with self.assertRaises(asyncio.CancelledError):
        await pool._prepare(None,a,False)
    sleep.assert_awaited_once_with(15)
    self.assertFalse(a.ready)

  async def test_stop_failure_still_starts_service(self):
    pool=self.pool(); a=pool.endpoints[0]
    pool._set_status=AsyncMock(side_effect=[TimeoutError(),None])
    with self.assertRaises(TimeoutError):
      await pool._restart(None,a,'vpn')
    self.assertEqual([c.args[2] for c in pool._set_status.await_args_list],['stopped','running'])

  async def test_control_key_only_on_private_requests_and_probes_use_proxy(self):
    class Response:
      async def __aenter__(self): return self
      async def __aexit__(self,*args): pass
      def raise_for_status(self): pass
      async def text(self): return '1.1.1.1'
      async def read(self): return b''
    calls=[]
    class Session:
      def get(self,*args,**kwargs): calls.append(('get',args,kwargs)); return Response()
      def head(self,*args,**kwargs): calls.append(('head',args,kwargs)); return Response()
      def put(self,*args,**kwargs): calls.append(('put',args,kwargs)); return Response()
    pool=self.pool(); a=pool.endpoints[0]; session=Session()
    await pool._probe(session,a)
    await pool._set_status(session,a,'running')
    for method,args,kwargs in calls:
      if method=='put':
        self.assertEqual(kwargs['headers'],{'X-API-Key':'test-key'})
        self.assertNotIn('proxy',kwargs)
      else:
        self.assertEqual(kwargs['proxy'],a.proxy_url)
        self.assertNotIn('headers',kwargs)

  def test_configuration_never_silently_falls_back(self):
    with patch.dict(os.environ,{'GLUETUN_CONTROL_URL':'http://legacy'},clear=True):
      with self.assertRaises(ValueError): VPNPool.from_env()
    with patch.dict(os.environ,{},clear=True): self.assertIsNone(VPNPool.from_env())
    with self.assertRaises(ValueError): VPNPool(CONFIG[:1],'key')

class PollTests(unittest.IsolatedAsyncioTestCase):
  async def exercise(self, error):
    pool=VPNPool(CONFIG,'key')
    a,b=pool.endpoints
    a.ready=b.ready=True; a.public_ip='1.1.1.1'; b.public_ip='2.2.2.2'
    @asynccontextmanager
    async def opened(session): yield
    pool.open=opened
    pool._schedule=lambda session,endpoint,rotate: setattr(endpoint,'ready',False)
    checks=[]
    async def check(*args,**kwargs):
      checks.append(kwargs)
      if len(checks)==1: raise error
      return {'model':('product',[],[])}
    telegram=AsyncMock()
    with patch('bot.VPNPool.from_env',return_value=pool), patch('bot.__get_status',side_effect=check), patch('bot.asyncio.sleep',side_effect=asyncio.CancelledError) as sleep:
      with self.assertRaises(asyncio.CancelledError):
        await getattr(bot,'__start')(telegram,{1},{'model'},'ca','Vancouver')
    return checks, pool, sleep

  async def test_541_immediately_retries_standby(self):
    error=aiohttp.ClientResponseError(SimpleNamespace(real_url="https://www.apple.com"),(),status=541)
    checks,pool,sleep=await self.exercise(error)
    self.assertEqual([c['proxy'] for c in checks],[CONFIG[0]['proxy_url'],CONFIG[1]['proxy_url']])
    self.assertEqual(sleep.await_count,1)
    self.assertIn('1.1.1.1',pool.blocked_ips)

  async def test_timeout_immediately_retries_standby(self):
    checks,pool,sleep=await self.exercise(TimeoutError())
    self.assertEqual(len(checks),2)
    self.assertEqual(pool.blocked_ips,{})

  async def test_429_honors_retry_after_without_switch(self):
    checks,pool,sleep=await self.exercise(aiohttp.ClientResponseError(SimpleNamespace(real_url="https://www.apple.com"),(),status=429,headers={'Retry-After':'121'}))
    self.assertEqual(len(checks),1)
    sleep.assert_awaited_once_with(121)
    self.assertTrue(pool.endpoints[0].ready)

if __name__=='__main__': unittest.main(verbosity=2)
