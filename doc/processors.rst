.. _processors:

Processors: Post-download actions
=================================

Post-download actions sometimes need to be taken on downloaded files
(unzipping, conversion to a more efficient format, etc).
If these actions are time or memory consuming, it might be worth doing them
only once after the file is downloaded.
This is a way of trading disk space for computation time.
:meth:`pooch.Pooch.fetch` and :func:`pooch.retrieve` accept the ``processor``
argument to handle these situations.

Processors are Python *callable objects*  (like functions or classes with a
``__call__`` method) that are executed after a file is downloaded to perform
these actions.
They must have the following format:

.. code:: python

    def myprocessor(fname, action, pooch):
        '''
        Processes the downloaded file and returns a new file name.

        The function **must** take as arguments (in order):

        fname : str
            The full path of the file in the local data storage
        action : str
            Either: "download" (file doesn't exist and will be downloaded),
            "update" (file is outdated and will be downloaded), or "fetch"
            (file exists and is updated so no download is necessary).
        pooch : pooch.Pooch
            The instance of the Pooch class that is calling this function.

        The return value can be anything but is usually a full path to a file
        (or list of files). This is what will be returned by Pooch.fetch and
        pooch.retrieve in place of the original file path.
        '''
        ...
        return full_path

The processor is executed after a file downloaded attempted (whether the
download actually happens or not) and before returning the path to the
downloaded file.
The processor lets us intercept the returned path, perform actions, and
possibly return a different path.

Pooch provides built-in processors for common tasks, like decompressing files
and unpacking tar and zip archives. See the :ref:`api` for a full list.

Common uses cases for processors include:

* :ref:`Unpacking archives <unpacking>` to load individual members
* :ref:`Decompressing <decompressing>` files


Processing manifests
--------------------

The built-in processors (:class:`pooch.Unzip`, :class:`pooch.Untar` and
:class:`pooch.Decompress`) keep a *processing manifest* next to each
processed file so that the derived (unpacked or decompressed) files can be
reused safely. A manifest is a small JSON file, named after the source file
with a ``.processing-<id>.json`` suffix, that registers three things:

* **the source verification**: the file name together with the hash
  algorithm, hash and size of the source file, taken from the same checksum
  that Pooch verifies against the registry before processing;
* **the processor identity**: the processor type and the parameters it was
  called with (for example the ``members`` or ``extract_dir`` of an
  :class:`pooch.Unzip`, or the ``method`` and ``name`` of a
  :class:`pooch.Decompress`);
* **the artifact list**: the full paths of every file produced by the
  processing, together with the version of the manifest format.

A cache hit is only served when the manifest marks the processing as
complete, the source file is still the exact same verified file, the
processor identity is unchanged, the manifest format is current and every
recorded artifact still exists on disk. This gives the following guarantees:

* The same verified source file is reused across calls.
* Processing the same source with different parameters writes different
  manifests, so derived files from different configurations are never mixed
  up.
* If the source is updated (its registry hash changes), the old derived
  files are replaced and the manifest is regenerated.
* If the processing fails, the partial outputs are removed and a ``failed``
  manifest is written. The half-finished result is therefore never treated
  as a cache hit, while the source file is kept untouched so that it can be
  processed again on the next call.

Plain processor functions that do not implement the manifest interface keep
working exactly as before and simply run on every call, as they always did.


Creating your own processors
----------------------------

Let's say we want to implement the :class:`pooch.Unzip` processor ourselves to
extract a single file from the archive. We could do that with the following
function:

.. code:: python

    import os
    from zipfile import ZipFile


    def unpack(fname, action, pup):
        """
        Post-processing hook to unzip a file and return the unzipped file name.

        Parameters
        ----------
        fname : str
           Full path of the zipped file in local storage
        action : str
           One of "download" (file doesn't exist and will download),
           "update" (file is outdated and will download), and
           "fetch" (file exists and is updated so no download).
        pup : Pooch
           The instance of Pooch that called the processor function.

        Returns
        -------
        fname : str
           The full path to the unzipped file. (Return the same fname is your
           processor doesn't modify the file).

        """
        # Create a new name for the unzipped file. Appending something to the
        # name is a relatively safe way of making sure there are no clashes
        # with other files in the registry.
        unzipped = fname + ".unzipped"
        # Don't unzip if file already exists and is not being downloaded
        if action in ("update", "download") or not os.path.exists(unzipped):
            with ZipFile(fname, "r") as zip_file:
                # Extract the data file from within the archive
                with zip_file.open("actual-data-file.txt") as data_file:
                    # Save it to our desired file name
                    with open(unzipped, "wb") as output:
                        output.write(data_file.read())
        # Return the path of the unzipped file
        return unzipped


    def fetch_zipped_file():
        """
        Load a large zipped sample data as a pandas.DataFrame.
        """
        # Pass in the processor to unzip the data file
        fname = GOODBOY.fetch("zipped-data-file.zip", processor=unpack)
        # fname is now the path of the unzipped file which can be loaded by
        # pandas directly
        data = pandas.read_csv(fname)
        return data


Similarly, you could build any custom processor function so long as it receives
the ``fname, action, pup`` arguments. Example use cases for this would be:

* Converting data from a download-friendly format (compressed and minimal file
  size) to a more user friendly format (easy to open and fast to load into
  memory).
* Add missing metadata to data from public servers. You might be using public
  data that has known issues (poorly formated entries, missing metadata, etc)
  which can be fixed when the file is downloaded.

The main advantage to using a processor for these actions is that they are
performed only when the file is downloaded. A modified version of the file can
be kept on disk so that loading the file is easier. This is particularly
convenient if the processor task takes a long time to run.
