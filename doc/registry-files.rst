.. _registryfiles:

Registry files
==============

Usage
-----

If your project has a large number of data files, it can be tedious to list
them in a dictionary. In these cases, it's better to store the file names and
hashes in a file and use :meth:`pooch.Pooch.load_registry` to read them.

.. code:: python

    import os
    from importlib import resources

    POOCH = pooch.create(
        path=pooch.os_cache("plumbus"),
        base_url="https://github.com/rick/plumbus/raw/{version}/data/",
        version=version,
        version_dev="main",
        # We'll load it from a file later
        registry=None,
    )
    # Get registry file from package_data
    registry_file = resources.open_text("plumbus", "registry.txt")
    # Load this registry file
    POOCH.load_registry(registry_file)

In this case, the ``registry.txt`` file is in the ``plumbus/`` package
directory and should be shipped with the package (see below for instructions).
We use `importlib.resources
<https://docs.python.org/3/library/importlib.resources.html>`__ to access the
``registry.txt``, giving it the name of our Python package.

Registry file format
--------------------

Registry files are light-weight text files that specify a file's name and hash.
In our example, the contents of ``registry.txt`` are:

.. code-block:: none

    c137.csv 19uheidhlkjdwhoiwuhc0uhcwljchw9ochwochw89dcgw9dcgwc
    cronen.csv 1upodh2ioduhw9celdjhlfvhksgdwikdgcowjhcwoduchowjg8w

A specific hashing algorithm can be enforced, if a checksum for a file is
prefixed with ``alg:``:

.. code-block:: none

    c137.csv sha1:e32b18dab23935bc091c353b308f724f18edcb5e
    cronen.csv md5:b53c08d3570b82665784cedde591a8b0

From Pooch v1.2.0 the registry file can also contain line comments, prepended
with a ``#``:

.. code-block:: none

    # C-137 sample data
    c137.csv 19uheidhlkjdwhoiwuhc0uhcwljchw9ochwochw89dcgw9dcgwc
    # Cronenberg sample data
    cronen.csv 1upodh2ioduhw9celdjhlfvhksgdwikdgcowjhcwoduchowjg8w

.. attention::

    Make sure you set the Pooch version in your ``setup.py`` to >=1.2.0 when
    using comments as earlier versions cannot handle them:
    ``install_requires = [..., "pooch>=1.2.0", ...]``


Packaging registry files
------------------------

To make sure the registry file is shipped with your package, include the
following in your ``MANIFEST.in`` file:

.. code-block:: none

    include plumbus/registry.txt

And the following entry in the ``setup`` function of your ``setup.py`` file:

.. code:: python

    setup(
        ...
        package_data={"plumbus": ["registry.txt"]},
        ...
    )


Creating a registry file
------------------------

If you have many data files, creating the registry and keeping it updated can
be a challenge. Function :func:`pooch.make_registry` will create a registry
file with all contents of a directory. For example, we can generate the
registry file for our fictitious project from the command-line:

.. code:: bash

   $ python -c "import pooch; pooch.make_registry('data', 'plumbus/registry.txt')"


Create registry file from remote files
--------------------------------------

If you want to create a registry file for a large number of data files that are
available for download but you don't have their hashes or any local copies,
you must download them first. Manually downloading each file
can be tedious. However, we can automate the process using
:func:`pooch.retrieve`. Below, we'll explore two different scenarios.

If the data files share the same base url, we can use :func:`pooch.retrieve`
to download them and then use :func:`pooch.make_registry` to create the
registry:

.. code:: python

    import os

    # Names of the data files
    filenames = ["c137.csv", "cronen.csv", "citadel.csv"]

    # Base url from which the data files can be downloaded from
    base_url = "https://www.some-data-hosting-site.com/files/"

    # Create a new directory where all files will be downloaded
    directory = "data_files"
    os.makedirs(directory)

    # Download each data file to data_files
    for fname in filenames:
        path = pooch.retrieve(
            url=base_url + fname, known_hash=None, fname=fname, path=directory
        )

    # Create the registry file from the downloaded data files
    pooch.make_registry("data_files", "registry.txt")

If each data file has its own url, the registry file can be manually created
after downloading each data file through :func:`pooch.retrieve`:

.. code:: python

    import os

    # Names and urls of the data files. The file names are used for naming the
    # downloaded files. These are the names that will be included in the registry.
    fnames_and_urls = {
        "c137.csv": "https://www.some-data-hosting-site.com/c137/data.csv",
        "cronen.csv": "https://www.some-data-hosting-site.com/cronen/data.csv",
        "citadel.csv": "https://www.some-data-hosting-site.com/citadel/data.csv",
    }

    # Create a new directory where all files will be downloaded
    directory = "data_files"
    os.makedirs(directory)

    # Create a new registry file
    with open("registry.txt", "w") as registry:
        for fname, url in fnames_and_urls.items():
            # Download each data file to the specified directory
            path = pooch.retrieve(
                url=url, known_hash=None, fname=fname, path=directory
            )
            # Add the name, hash, and url of the file to the new registry file
            registry.write(
                f"{fname} {pooch.file_hash(path)} {url}\n"
            )

.. warning::

    Notice that there are **no checks for download integrity** (since we don't
    know the file hashes before hand). Only do this for trusted data sources
    and over a secure connection. If you have access to file hashes/checksums,
    **we highly recommend using them** to set the ``known_hash`` argument.


Versioned registries
--------------------

A registry is treated as a **versioned document**.  Each time you load one, a
complete, immutable snapshot is published.  A call to
:meth:`pooch.Pooch.fetch` takes one snapshot before it starts and uses that
snapshot for the whole operation, including the checksum and the download URL.
Refreshing the registry from another thread or process therefore cannot change
the meaning of a fetch that is already running.

.. code:: python

    POOCH = pooch.create(
        path=pooch.os_cache("plumbus"),
        base_url="https://github.com/rick/plumbus/raw/{version}/data/",
        version=version,
        registry=None,
    )
    # A loaded registry is a complete version, not a patch to the old one.
    POOCH.load_registry(registry_file, version="2026.11.1")

    print(POOCH.registry_version)  # 2026.11.1
    print(POOCH.registry_digest)    # sha256:... (content identity)

If you do not supply a version label, the content digest becomes the version.
This gives every distinct registry a traceable identity without claiming a
release number it does not have.  The digest ignores comments and line order
but changes whenever a checksum, URL, or retraction changes.

Previous snapshots remain available through
:attr:`pooch.Pooch.registry_history` and :attr:`pooch.Pooch.registry_versions`
and can be used explicitly:

.. code:: python

    # Fetch with the checksum and URLs of the 2026.11.1 registry
    path = POOCH.fetch_version("c137.csv", version="2026.11.1")

    # Inspect the snapshot without changing the current one
    old = POOCH.registry_at("2026.11.1")
    print(old.files)

Refreshing a registry is **atomic**.  The complete new file is parsed and
validated first; if anything is wrong, the current registry is left untouched.
A failed refresh never publishes a partial registry:

.. code:: python

    def download_registry(fileobj):
        response = requests.get(registry_url, timeout=30)
        response.raise_for_status()
        fileobj.write(response.content)

    try:
        POOCH.refresh_registry(download_registry, version="2026.12.0")
    except OSError:
        # The old registry is still intact and still in use.
        pass

:meth:`~pooch.Pooch.load_registry` treats the file as a complete new registry
by default.  Pass ``replace=False`` when the file only adds entries to the
existing registry.

Retractions are explicit
------------------------

A file that is **missing** from a new registry is simply no longer declared.
That is not the same as a file that has been **retracted**.  A retraction means
that the registry explicitly declares the file as withdrawn, and trying to
fetch it raises :class:`pooch.RetractedFileError` instead of the
:class:`pooch.FileNotInRegistryError` used for an ordinary omission.  Both are
subclasses of :class:`ValueError`.

Retractions are written on their own line, using any of these forms:

.. code-block:: none

    adios.csv retracted replaced by c137-v2.csv
    adios.csv withdrawn because of a calibration error
    !adios.csv revoked
    - adios.csv superseded

A later normal entry for the same file revives it:

.. code-block:: none

    adios.csv 19uheidhlkjdwhoiwuhc0uhcwljchw9ochwochw89dcgw9dcgwc

You can also retract a file programmatically:

.. code:: python

    POOCH.retract("cronen.csv", reason="replaced by the revised dataset")

Preserving old content
----------------------

When a registry refresh changes a checksum, omits a declared file, or retracts
one, local content that matches the **old** declaration is copied into a
content-addressed history folder before it can be overwritten:

.. code-block:: none

    ~/.cache/plumbus/v1.0/.pooch-history/
        index.json
        sha256/
            <digest-of-old-content>

Each entry in ``index.json`` records the original file name, the old checksum,
the registry version under which it was valid, when it was preserved, and why
it was superseded (``superseded``, ``undeclared``, or ``retracted``).  Old
content therefore keeps a stable, traceable identity without being presented
as the content of the new registry.  The same records are available in Python
through :attr:`pooch.Pooch.provenance`.  The index and the registry versions it
references are loaded again automatically, so a new Python process can still
resolve ``fetch_version`` against an earlier snapshot.
