Scraping
=========

..
  >>> from pyquery import PyQuery as pq
  >>> your_url = getfixture('scrap_url')

PyQuery is able to load an html document from a url::

  >>> pq(url=your_url)
  [<html>]

By default it uses python's urllib.

If `requests`_ is installed then it will use it. This allow you to use most of `requests`_ parameters::

  >>> pq(url=your_url, headers={'user-agent': 'pyquery'})
  [<html>]

  >>> pq(url=your_url, data={'q': 'foo'}, method='post', verify=True)
  [<html>]


Timeout
-------

The default timeout is 60 seconds, you can change it by setting the timeout parameter which is forwarded to the underlying urllib or requests library.

Session
-------

When using the requests library you can instantiate a Session object which keeps state between http calls (for example - to keep cookies). You can set the session parameter to use this session object.

Streaming input
---------------

A response body that can only be read once can be handed over directly.
Pass it as the ``stream`` keyword, as the only positional argument, or use
``PyQuery.fromstream()``.  Only what the parser asks for is read from the
source; the data is cached along the way so the XML-to-HTML fallback can
replay it from the beginning without touching the source again::

  >>> from io import BytesIO
  >>> from pyquery import PyQuery as pq
  >>> stream = BytesIO(b'<root><p>hello</p></root>')
  >>> pq(stream=stream, parser='xml')('p').text()
  'hello'
  >>> stream.closed
  False

The stream is borrowed: the caller remains responsible for closing it.
Internal caches and any temporary file are released whether parsing
succeeds, fails, or reading raises.

``cache_limit`` caps the in-memory cache in bytes.  By default exceeding
it raises ``StreamCacheLimitError``; pass ``allow_tempfile=True`` to spill
the overflow to an anonymous temporary file instead::

  >>> pq(stream=BytesIO(b'<root>' + b'x' * 100 + b'</root>'),
  ...    cache_limit=16, allow_tempfile=True)('root').text()
  'xxxx...'

.. _requests: http://docs.python-requests.org/en/latest/
