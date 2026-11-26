"""Direct HTTP opener for provider transport and discovery (#78).

The default urllib opener captures the ambient proxy map the first time a
request is made (lazily built) and never refreshes it. A
stopped proxy can then break provider calls, including loopback endpoints,
until the process restarts. An explicit empty proxy map avoids that
inheritance without changing the global opener for other consumers.
"""
from urllib.request import ProxyHandler, build_opener

NO_PROXY_OPENER = build_opener(ProxyHandler({}))
