Manipulating
------------

..
    >>> from pyquery import PyQuery as pq

You can also add content to the end of tags::

    >>> d = pq('<p class="hello" id="hello">you know Python rocks</p>')
    >>> d('p').append(' check out <a href="http://reddit.com/r/python"><span>reddit</span></a>')
    [<p#hello.hello>]
    >>> print(d)
    <p class="hello" id="hello">you know Python rocks check out <a href="http://reddit.com/r/python"><span>reddit</span></a></p>

Or to the beginning::

    >>> p = d('p')
    >>> p.prepend('check out <a href="http://reddit.com/r/python">reddit</a>')
    [<p#hello.hello>]
    >>> print(p.html())
    check out <a href="http://reddit.com/r/python">reddit</a>you know ...

Prepend or append an element into an other::

    >>> d = pq('<html><body><div id="test"><a href="http://python.org">python</a> !</div></body></html>')
    >>> p.prependTo(d('#test'))
    [<p#hello.hello>]
    >>> print(d('#test').html())
    <p class="hello" ...

Insert an element after another::

    >>> p.insertAfter(d('#test'))
    [<p#hello.hello>]
    >>> print(d('#test').html())
    <a href="http://python.org">python</a> !

Or before::

    >>> p.insertBefore(d('#test'))
    [<p#hello.hello>]
    >>> print(d('body').html())
    <p class="hello" id="hello">...

Doing something for each elements::

    >>> p.each(lambda i, e: pq(e).addClass('hello2'))
    [<p#hello.hello.hello2>]

Remove an element::

    >>> d = pq('<html><body><p id="id">Yeah!</p><p>python rocks !</p></div></html>')
    >>> d.remove('p#id')
    [<html>]
    >>> d('p#id')
    []

Remove what's inside the selection::

    >>> d('p').empty()
    [<p>]

And you can get back the modified html::

    >>> print(d)
    <html><body><p/></body></html>

You can generate html stuff::

    >>> from pyquery import PyQuery as pq
    >>> print(pq('<div>Yeah !</div>').addClass('myclass') + pq('<b>cool</b>'))
    <div class="myclass">Yeah !</div><b>cool</b>

Remove all namespaces::

    >>> d = pq('<foo xmlns="http://example.com/foo"></foo>')
    >>> d
    [<{http://example.com/foo}foo>]
    >>> d.remove_namespaces()
    [<foo>]

Merge partial page fragments
----------------------------

Fragments often contain only the records seen in one page.  Use
``merge_fragment`` to update one replacement region without treating a
short fragment as an authoritative snapshot of the whole region::

    >>> d = pq('<ul id="open"><li id="a">A</li><li id="b">B</li>'
    ...        '<li id="c">C</li></ul>')
    >>> d.merge_fragment('<li id="b">B2</li><li id="d">D</li>', '#open')
    [<ul#open>]
    >>> print(d('#open').html())
    <li id="a">A</li><li id="b">B2</li><li id="d">D</li><li id="c">C</li>

Records are matched by a unique identity key (``id`` by default) and are
updated in place, so references to the existing elements stay valid.
Records without a usable key, and records whose key is ambiguous on either
side, are kept as independent occurrences; equal values alone do not make
two rows the same record::

    >>> d = pq('<ul id="rows"><li>A</li></ul>')
    >>> d.merge_fragment('<li>A</li><li>A</li>', '#rows')
    [<ul#rows>]
    >>> print(d('li').length)
    3

Pass ``key=`` to use another attribute or a callable, and ``selector=`` to
restrict which children of the region are records.  Only the target region
is modified, so a fragment taken from another region can be reused::

    >>> d = pq('<div><ul id="one"><li id="x">X</li></ul>'
    ...        '<ul id="two"><li id="x">X</li></ul></div>')
    >>> d.merge_fragment(d('#two li'), '#one')
    [<div>]
    >>> print(d('#two').html())
    <li id="x">X</li>

The default is additive.  If the fragment is known to be complete for the
region, pass ``complete=True``; every old record not matched by a unique key
is then removed.  The removal is refused when any incoming record lacks a
unique key, because a truncated fragment must not wipe the region.  An empty
fragment removes nothing unless ``allow_empty=True`` is also passed::

    >>> d = pq('<ul id="rows"><li id="a">A</li><li id="b">B</li></ul>')
    >>> d.merge_fragment('<li id="a">A2</li>', '#rows', complete=True)
    [<ul#rows>]
    >>> print(d('#rows').html())
    <li id="a">A2</li>

Use ``merge_fragments`` for several pages at once; each item is a
``(fragment, region)`` tuple or a dictionary with ``fragment`` and
``region`` keys.  Batches are applied in order, so a later page can update a
record added by an earlier one.

