Python Tutorial
===============

This is a brief overview of how to use the *dcmstack* Python package. For 
details refer to :doc:`modules`.

Creating DicomStack Objects
---------------------------

If you have an acquisition that you would like to turn into a single 
*DicomStack* object then you may want to do this directly.

.. code-block:: python
    
    >>> import dcmstack, pydicom
    >>> from glob import glob
    >>> src_paths = glob('032-MPRAGEAXTI900Pre/*.dcm')
    >>> my_stack = dcmstack.DicomStack()
    >>> for src_path in src_paths:
    ...     src_dcm = pydicom.dcmread(src_path)
    ...     my_stack.add_dcm(src_dcm)

If you are unsure how many stacks you want from a collection of DICOM data 
sets then you should use the *parse_and_stack* function. This will group 
together data sets from the same DICOM series.

.. code-block:: python
    
    >>> import dcmstack
    >>> from glob import glob
    >>> src_paths = glob('dicom_data/*.dcm')
    >>> stacks = dcmstack.parse_and_stack(src_paths)
    
Any keyword arguments for the *DicomStack* constructor can also be passed 
to *parse_and_stack*.


Specifying Time and Vector Order
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

By default, if there is more than one 3D volume in the stack the software 
will try to guess the meta key to sort the fourth (time) dimension. To 
specify the meta data key for the fourth dimension or stack along the fifth 
(vector) dimension, use the *time_order* and *vector_order* arguments to the 
*DicomStack* constructor. 

Grouping Datasets
^^^^^^^^^^^^^^^^^

The *parse_and_stack* function groups data sets using a tuple of meta data 
keys provided as the argument *group_by*. The default values should group 
datasets from the same series into the same stack. The result is a 
dictionary where the keys are the matching tuples of meta data values, and 
the values are the are the corresponding stacks.

Using DicomStack Objects
------------------------

Once you have created your *DicomStack* objects you will typically want to get 
the array of voxel data, get the affine transform, or create a Nifti1Image.

.. code-block:: python
    
    >>> stack_data = my_stack.get_data()
    >>> stack_affine = my_stack.affine
    >>> nii = my_stack.to_nifti()
    
Embedding Meta Data
^^^^^^^^^^^^^^^^^^^

The meta data from the source DICOM data sets can be summarized into a 
*DcmMetaExtension* which is embedded into the Nifti header. To do this you can 
either pass True for the *embed_meta* parameter to *DicomStack.to_nifti* or 
you can immediately get a *NiftiWrapper* with *DicomStack.to_nifti_wrapper*.

By default the meta data is filtered to reduce the chance of including 
private health information.  This filtering can be controlled with the 
*meta_filter* parameter to the *DicomStack* constructor.

**IT IS YOUR RESPONSIBILITY TO KNOW IF THERE IS PRIVATE HEALTH INFORMATION 
IN THE RESULTING FILE AND TREAT SUCH FILES APPROPRIATELY.**

Creating NiftiWrapper Objects
-----------------------------

The *NiftiWrapper* class can be used to work with extended Nifti files. 
It wraps a *Nifti1Image* from the *nibabel* package. As mentioned above, 
these can be created directly from a *DicomStack*.

.. code-block:: python
    
    >>> import dcmstack, pydicom
    >>> from glob import glob
    >>> src_paths = glob('032-MPRAGEAXTI900Pre/*.dcm')
    >>> my_stack = dcmstack.DicomStack()
    >>> for src_path in src_paths:
    ...     src_dcm = pydicom.dcmread(src_path)
    ...     my_stack.add_dcm(src_dcm)
    ...
    >>> nii_wrp = my_stack.to_nifti_wrapper()
    >>> nii_wrp.get_meta('InversionTime')
    900.0

They can also be created by passing a *Nifti1Image* to the *NiftiWrapper* 
constructor or by passing the path to a Nifti file to 
*NiftiWrapper.from_filename*. 

Using NiftiWrapper Objects
--------------------------

The *NiftiWrapper* objects have attribute *nii_img* pointing to the 
*Nifti1Image* being wrapped and the attribute *meta_ext* pointing to the 
*DcmMetaExtension*. There are also a number of methods for working with 
the image data and meta data together. For example merging or splitting 
the data set along the time axis.

Looking Up Meta Data
^^^^^^^^^^^^^^^^^^^^
Meta data that is constant can be accessed with dict-style lookups. The more 
general access method is *get_meta* which can optionally take an index into 
the voxel array in order to provide access to varying meta data.

.. code-block:: python
    
    >>> nii_wrp = NiftiWrapper.from_filename('032-MPRAGEAXTI900Pre.nii.gz')
    >>> nii_wrp['InversionTime']
    900.0
    >>> nii_wrp.get_meta('InversionTime')
    900.0
    >>> nii_wrp['InstanceNumber']
    Traceback (most recent call last):
      File "<stdin>", line 1, in <module>
      File "build/bdist.linux-x86_64/egg/dcmstack/dcmmeta.py", line 1026, in __getitem__
    KeyError: 'InstanceNumber'
    >>> nii_wrp.get_meta('InstanceNumber')
    >>> nii_wrp.get_meta('InstanceNumber', index=(0,0,0))
    1
    >>> nii_wrp.get_meta('InstanceNumber', index=(0,0,1))
    2

Merging and Splitting Data Sets
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
We can create a *NiftiWrapper* by merging a sequence of *NiftiWrapper*
objects using the class method *from_sequence*. Conversely, we can split 
a *NiftiWrapper* into a sequence if *NiftiWrapper* objects using the 
method *split*.

.. code-block:: python
    
    >>> from dcmstack.dcmmeta import NiftiWrapper
    >>> nw1 = NiftiWrapper.from_filename('img1.nii.gz')
    >>> nw2 = NiftiWrapper.from_filename('img2.nii.gz')
    >>> print nw1.nii_img.shape
    (384, 512, 60)
    >>> print nw2.nii_img.shape
    (384, 512, 60)
    >>> print nw1.get_meta('EchoTime')
    11.0
    >>> print nw2.get_meta('EchoTime')
    87.0
    >>> merged = NiftiWrapper.from_sequence([nw1, nw2])
    >>> print merged.nii_img.shape
    (384, 512, 60, 2)
    >>> print merged.get_meta('EchoTime', index=(0,0,0,0)
    11.0
    >>> print merged.get_meta('EchoTime', index=(0,0,0,1)
    87.0
    >>> splits = list(merge.split())
    >>> print splits[0].nii_img.shape
    (384, 512, 60)
    >>> print splits[1].nii_img.shape
    (384, 512, 60)
    >>> print splits[0].get_meta('EchoTime')
    11.0
    >>> print splits[1].get_meta('EchoTime')
    87.0

Accessing the the DcmMetaExtension
----------------------------------

It is generally recommended that meta data is accessed through the 
*NiftiWrapper* class since it can do some checks between the meta data
and the image data. For example, it will make sure the dimensions and 
slice direction have not changed before using varying meta data.

However certain actions are much easier when accessing the meta data 
extension directly.

.. code-block:: python
    
    >>> from dcmstack.dcmmeta import NiftiWrapper
    >>> nw1 = NiftiWrapper.from_filename('img.nii.gz')
    >>> nw.meta_ext.shape
    >>> (384, 512, 60, 2)
    >>> print nw.meta_ext.get_values('EchoTime')
    [11.0, 87.0]
    >>> print nw.meta_ext.get_classification('EchoTime')
    ('time', 'samples')

Diffusion Gradients and Reorientation
-------------------------------------

When a stack is converted with a voxel order (reorientation), the image
voxels, affine and the meta data are updated together.  The original DICOM
gradient directions can be exported alongside the image with
*DicomStack.get_gradients* or *NiftiWrapper.get_gradients*:

.. code-block:: python

    >>> nii_wrp = my_stack.to_nifti_wrapper('LAS')
    >>> table = nii_wrp.get_gradients(
    ...     source_basis='scanner', target_basis='voxel',
    ...     gradient_keys=('DiffusionGradientOrientation',),
    ...     b_value_keys=('DiffusionBValue',),
    ... )
    >>> table.bvals
    array([0., 1000., 2000.])
    >>> table.bvecs
    array([[...]])
    >>> table.instances
    ['1.2.3.1', ...]

Each row of the table corresponds to one actual sample.  The b-value, the
source meta data key, the source instance(s) and the source index or indices
are kept with the gradient so the table can be aligned with the voxel data.
If the gradient or b-value varies from slice to slice within a volume, one
row is produced per slice instead of silently discarding values.

Directions are interpreted in the *source_basis* and written in the
*target_basis*:

- ``scanner`` is the DICOM patient basis (LPS by default)
- ``world`` is the NIfTI world basis (RAS by default)
- ``voxel`` uses the affine column vectors

A voxel reorientation permutes and flips the voxel axes, but it does not
rotate a physical direction as a side effect.  Converting a ``scanner`` or
``world`` direction to ``voxel`` after reorientation therefore uses the new
affine, while a direction supplied in the ``voxel`` basis is permuted/flipped
directly.  The ``source_convention`` and ``target_convention`` arguments can
be used when the scanner/world data use the other handedness.

A missing or incomplete direction is never replaced by a unit vector.  Zero
gradients stay zero and are marked with status ``zero``.  A missing direction
has status ``missing`` (or ``incomplete`` when fewer than three values were
available).  Degenerate affines raise *DegenerateTransformError* by default;
with ``on_degenerate='status'`` the affected row is marked ``degenerate`` and
its gradient is left as ``nan``.

The table also supports returning to the original basis:

.. code-block:: python

    >>> original = table.inverse('scanner')
    >>> np.allclose(original.gradients, source_gradients)
    True

The inverse preserves the b-values, source instances and source indices.

Use *table.write_json(path)* for the full table, or
*gradients.write_bvals_bvecs* (or *table.write_json* plus the CLI options
described in :doc:`CLI_Tutorial`) for FSL-style ``.bval``/``.bvec`` files.  A
missing direction is written as zeros in those two files; use the JSON table
to distinguish it from a genuine zero gradient.
