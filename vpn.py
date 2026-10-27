"""Two warm Gluetun routes; failed routes recover outside the polling loop."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import ipaddress
import json
import logging
import os
import time
from urllib.parse import urlsplit

import aiohttp


@dataclass
class Endpoint:
  name: str
  control_url: str
  proxy_url: str
  public_ip: str = ''
  ready: bool = False
  task: asyncio.Task | None = None


class VPNPool:
  COOLDOWN = 30 * 60
  READY_TIMEOUT = 90

  def __init__(self, endpoints, api_key, hostname='www.apple.com'):
    self.endpoints = [Endpoint(**entry) for entry in endpoints]
    if len(self.endpoints) != 2:
      raise ValueError('GLUETUN_ENDPOINTS must contain exactly two routes')
    for field in ('name', 'control_url', 'proxy_url'):
      values = [getattr(endpoint, field) for endpoint in self.endpoints]
      if any(not isinstance(value, str) or not value for value in values) or len(set(values)) != 2:
        raise ValueError(f'VPN routes need distinct, nonempty {field} values')
    for endpoint in self.endpoints:
      for url in (endpoint.control_url, endpoint.proxy_url):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password:
          raise ValueError('VPN URLs must be private HTTP addresses without credentials')
    self.api_key = api_key
    self.hostname = hostname
    self.blocked_ips = {}
    self.changed = asyncio.Event()
    self.active = None

  @classmethod
  def from_env(cls, hostname='www.apple.com'):
    config = os.environ.get('GLUETUN_ENDPOINTS')
    if not config:
      if os.environ.get('GLUETUN_CONTROL_URL'):
        raise ValueError('Replace GLUETUN_CONTROL_URL with two GLUETUN_ENDPOINTS; see VPN.md')
      return None
    key = os.environ.get('GLUETUN_API_KEY')
    if not key:
      raise ValueError('GLUETUN_API_KEY is required with GLUETUN_ENDPOINTS')
    return cls(json.loads(config), key, hostname)

  @asynccontextmanager
  async def open(self, session):
    try:
      for endpoint in self.endpoints:
        self._schedule(session, endpoint, rotate=False)
      yield self
    finally:
      tasks = [endpoint.task for endpoint in self.endpoints if endpoint.task]
      for task in tasks:
        task.cancel()
      await asyncio.gather(*tasks, return_exceptions=True)

  async def _set_status(self, session, endpoint, status, service='vpn'):
    # Control calls stay on the private Docker network. Never send this key via a proxy.
    async with session.put(
        f'{endpoint.control_url.rstrip("/")}/v1/{service}/status',
        json={'status': status}, headers={'X-API-Key': self.api_key},
        timeout=aiohttp.ClientTimeout(total=20)) as response:
      response.raise_for_status()
      await response.read()

  async def _restart(self, session, endpoint, service):
    try:
      await self._set_status(session, endpoint, 'stopped', service)
    finally:
      await self._set_status(session, endpoint, 'running', service)

  async def _probe(self, session, endpoint):
    # Probe the actual proxy route, not Gluetun's cached public-IP metadata.
    async with session.get('https://api.ipify.org', proxy=endpoint.proxy_url,
                           timeout=aiohttp.ClientTimeout(total=10)) as response:
      response.raise_for_status()
      public_ip = str(ipaddress.ip_address((await response.text()).strip()))
    # This validates remote DNS/TLS readiness, not pickup eligibility for this IP.
    async with session.head(f'https://{self.hostname}/', proxy=endpoint.proxy_url,
                            timeout=aiohttp.ClientTimeout(total=10)) as response:
      await response.read()
    return public_ip

  async def _wait_for_route(self, session, endpoint):
    async with asyncio.timeout(self.READY_TIMEOUT):
      while True:
        try:
          return await self._probe(session, endpoint)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
          await asyncio.sleep(2)

  def _schedule(self, session, endpoint, rotate):
    endpoint.ready = False
    if endpoint.task is None or endpoint.task.done():
      endpoint.task = asyncio.create_task(self._prepare(session, endpoint, rotate),
                                          name=f'vpn-repair-{endpoint.name}')

  async def _prepare(self, session, endpoint, rotate):
    delay = 15
    while True:
      started = time.monotonic()
      try:
        if rotate:
          logging.info('VPN %s reconnecting in background; previous IP=%s',
                       endpoint.name, endpoint.public_ip or 'unknown')
          await self._restart(session, endpoint, 'vpn')
          await self._restart(session, endpoint, 'dns')
        public_ip = await self._wait_for_route(session, endpoint)
        now = time.monotonic()
        self.blocked_ips = {ip: until for ip, until in self.blocked_ips.items() if until > now}
        if public_ip in self.blocked_ips:
          raise ValueError('recently blocked IP')
        if any(other is not endpoint and other.ready and other.public_ip == public_ip
               for other in self.endpoints):
          raise ValueError('same IP as the other ready VPN')
        endpoint.public_ip = public_ip
        endpoint.ready = True
        self.changed.set()
        logging.info('VPN %s ready: public IP=%s (%.1f seconds)',
                     endpoint.name, public_ip, now - started)
        return
      except asyncio.CancelledError:
        raise
      except Exception as error:
        # Own every background failure, without exposing response bodies or secrets.
        logging.warning('VPN %s recovery failed (%s); retrying in %s seconds',
                        endpoint.name, type(error).__name__, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)
        rotate = True

  def fail(self, session, endpoint, blocked=False):
    if blocked and endpoint.public_ip:
      self.blocked_ips[endpoint.public_ip] = time.monotonic() + self.COOLDOWN
    logging.warning('VPN %s unavailable (%s); selecting standby while it reconnects',
                    endpoint.name, 'HTTP 541' if blocked else 'transport failure')
    self._schedule(session, endpoint, rotate=True)

  async def route(self):
    while True:
      self.changed.clear()
      if self.active is not None and self.active.ready:
        return self.active
      for endpoint in self.endpoints:
        if endpoint.ready:
          previous = self.active.name if self.active else 'none'
          self.active = endpoint
          logging.info('VPN route switched: %s -> %s (IP=%s)', previous,
                       endpoint.name, endpoint.public_ip)
          return endpoint
      logging.warning('Waiting for a ready VPN; keeping last confirmed pickup results')
      try:
        await asyncio.wait_for(self.changed.wait(), timeout=30)
      except asyncio.TimeoutError:
        pass
