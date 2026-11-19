#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdbool.h>
#include <stdint.h>

typedef struct {
    PyObject_HEAD
    PyObject *read;    // cached read() method of fp
    PyObject *tag_hook;
    PyObject *object_hook;
    PyObject *object_pairs_hook;
    PyObject *shareables;
    PyObject *stringref_namespace;
    PyObject *str_errors;
    bool immutable;
    bool map_pairs;
    bool key_to_str;
    Py_ssize_t shared_index;
} CBORDecoderObject;

extern PyTypeObject CBORDecoderType;

PyObject * CBORDecoder_new(PyTypeObject *, PyObject *, PyObject *);
int CBORDecoder_init(CBORDecoderObject *, PyObject *, PyObject *);
PyObject * CBORDecoder_decode(CBORDecoderObject *);
