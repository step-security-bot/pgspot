from copy import copy
from pglast.stream import RawStream
from pglast import ast


def raw_sql(node):
    return RawStream()(node)


def get_text(node):
    match (node):
        case str():
            return node
        case ast.A_Const():
            return get_text(node.val)
        case ast.String():
            return node.sval
        case _:
            return str(node)


def format_name(name):
    match (name):
        case str():
            return name
        case (list() | tuple()):
            return ".".join([format_name(p) for p in name])
        case ast.String():
            return name.sval
        case ast.RangeVar():
            if name.schemaname:
                return f"{name.schemaname}.{name.relname}"
            return name.relname
        case ast.TypeName():
            return ".".join([format_name(p) for p in name.names])
        case _:
            return str(name)


def format_function(node):
    args = []
    if node.parameters:
        for p in node.parameters:
            arg_copy = copy(p)
            # strip out default expressions
            arg_copy.defexpr = None
            args.append(raw_sql(arg_copy))

    return f"{format_name(node.funcname)}({','.join(args)})"


def format_aggregate(node):
    if node.oldstyle:
        basetype = [b.arg.names for b in node.definition if b.defname == "basetype"]
        if basetype:
            basetype = basetype[0]

        if not basetype:
            args = ""
        elif len(basetype) == 2 and basetype[0].sval == "pg_catalog":
            args = basetype[1].sval
        else:
            args = ",".join([s.sval for s in basetype])
    else:
        args = ",".join([raw_sql(arg.argType) for arg in node.args[0]])
    return f"{format_name(node.defnames)}({args})"
