'''Generate Cython PYX wrappers for Boost stats distributions.'''

from typing import NamedTuple
from warnings import warn
from textwrap import dedent
import pathlib
import argparse

from gen_func_defs_pxd import (  # type: ignore
    _gen_func_defs_pxd)
from _info import (  # type: ignore
    _x_funcs, _no_x_funcs, _klass_mapper)


class _MethodDef(NamedTuple):
    ufunc_name: str
    num_inputs: int
    boost_func_name: str


def _ufunc_gen(scipy_dist: str, types: list, ctor_args: tuple,
               filename: str, boost_dist: str, x_funcs: list,
               no_x_funcs: list):
    '''
    We need methods defined for each rv_continuous/_discrete internal method:
        i.e.: _pdf, _cdf, etc.
    Some of these methods take constructor arguments and 1 extra argument,
        e.g.: _pdf(x, *ctor_args), _ppf(q, *ctor_args)
    while some of the methods take only constructor arguments:
        e.g.: _stats(*ctor_args)
    '''
    num_ctor_args = len(ctor_args)
    methods = [_MethodDef(
        ufunc_name=f'_{scipy_dist}_{x_func}',
        num_inputs=num_ctor_args+1,  # +1 for the x argument
        # PDF for the beta distribution has a custom wrapper:
        boost_func_name=x_func if boost_dist != 'beta_distribution'
        else 'pdf_beta' if x_func == 'pdf' else x_func,
    ) for x_func in x_funcs]
    methods += [_MethodDef(
        ufunc_name=f'_{scipy_dist}_{func}',
        num_inputs=num_ctor_args,
        boost_func_name=func,
    ) for func in no_x_funcs]

    # Identify potential ufunc issues:
    no_input_methods = [m for m in methods if m.num_inputs == 0]
    if no_input_methods:
        raise ValueError("ufuncs must have >0 arguments! "
                         f"Cannot construct these ufuncs: {no_input_methods}")

    boost_hdr_name = boost_dist.split('_distribution')[0]
    unique_num_inputs = set({m.num_inputs for m in methods})
    has_NPY_FLOAT16 = 'NPY_FLOAT16' in types
    line_joiner = ',\n    ' + ' '*12
    num_types = len(types)
    loop_fun = 'PyUFunc_T'
    func_defs_cimports = line_joiner.join(
        f"boost_{m.boost_func_name}{num_ctor_args}" for m in methods)
    nontype_params = line_joiner[1:].join(
        f'ctypedef int NINPUTS{n} "{n}"' for n in unique_num_inputs)

    with open(filename, 'w') as fp:
        boost_hdr = f'boost/math/distributions/{boost_hdr_name}.hpp'
        relimport = '.'

        fp.write(dedent(f'''\
            # cython: language_level=3

            # This file was generated by stats/_boost/include/code_gen.py
            # All modifications to this file will be overwritten.

            from numpy cimport (
                import_array,
                import_ufunc,
                PyUFunc_FromFuncAndData,
                PyUFuncGenericFunction,
                PyUFunc_None,
                {line_joiner.join(types)}
            )
            from {relimport}templated_pyufunc cimport PyUFunc_T
            from {relimport}func_defs cimport (
                {func_defs_cimports},
            )
            cdef extern from "{boost_hdr}" namespace "boost::math" nogil:
                cdef cppclass {boost_dist} nogil:
                    pass

            # Workaround for Cython's lack of non-type template parameter
            # support
            cdef extern from * nogil:
                {nontype_params}

            _DUMMY = ""
            import_array()
            import_ufunc()
            '''))

        if has_NPY_FLOAT16:
            warn('Boost stats NPY_FLOAT16 ufunc generation not '
                 'currently not supported!')

        # Generate ufuncs for each method
        for ii, m in enumerate(methods):
            fp.write(dedent(f'''
                cdef PyUFuncGenericFunction loop_func{ii}[{num_types}]
                cdef void* func{ii}[1*{num_types}]
                cdef char types{ii}[{m.num_inputs+1}*{num_types}]
                '''))  # m.num_inputs+1 for output arg

            for jj, T in enumerate(types):
                ctype = {
                    'NPY_DOUBLE': 'double',
                    'NPY_FLOAT': 'float',
                    'NPY_FLOAT16': 'npy_half',
                }[T]
                boost_fun = f'boost_{m.boost_func_name}{num_ctor_args}'
                type_str = ", ".join([ctype]*(1+num_ctor_args))
                boost_tmpl = f'{boost_dist}, {type_str}'
                N = m.num_inputs
                fp.write(f'''\
loop_func{ii}[{jj}] = <PyUFuncGenericFunction>{loop_fun}[{ctype}, NINPUTS{N}]
func{ii}[{jj}] = <void*>{boost_fun}[{boost_tmpl}]
''')
                for tidx in range(m.num_inputs+1):
                    fp.write(
                        f'types{ii}[{tidx}+{jj}*{m.num_inputs+1}] = {T}\n')
            arg_list_str = ', '.join(ctor_args)
            if m.boost_func_name in x_funcs:
                arg_list_str = 'x, ' + arg_list_str
            fp.write(dedent(f'''
                {m.ufunc_name} = PyUFunc_FromFuncAndData(
                    loop_func{ii},
                    func{ii},
                    types{ii},
                    {num_types},  # number of supported input types
                    {m.num_inputs},  # number of input args
                    1,  # number of output args
                    PyUFunc_None,  # `identity` element, never mind this
                    "{m.ufunc_name}",  # function name
                    ("{m.ufunc_name}({arg_list_str}) -> computes "
                     "{m.boost_func_name} of {scipy_dist} distribution"),
                    0  # unused
                )
                '''))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--outdir", type=str,
                        help="Path to the output directory")
    args = parser.parse_args()

    _boost_dir = pathlib.Path(__file__).resolve().parent.parent
    if not args.outdir:
        raise ValueError("A path to the output directory is required")
    else:
        src_dir = pathlib.Path(args.outdir)

    # generate the PXD and PYX wrappers
    _gen_func_defs_pxd(
        f'{src_dir}/func_defs.pxd',
        x_funcs=_x_funcs,
        no_x_funcs=_no_x_funcs)
    float_types = ['NPY_FLOAT', 'NPY_DOUBLE']
    for b, s in _klass_mapper.items():
        _ufunc_gen(
            scipy_dist=s.scipy_name,
            types=float_types,
            ctor_args=s.ctor_args,
            filename=f'{src_dir}/{s.scipy_name}_ufunc.pyx',
            boost_dist=f'{b}_distribution',
            x_funcs=_x_funcs,
            no_x_funcs=_no_x_funcs,
        )
