
from pglast import ast, parse_sql, parse_plpgsql
from pglast.visitors import Visitor
from pglast.enums.parsenodes import VariableSetKind
from formatters import raw_sql, format_name, format_function
import re

def visit_sql(state, sql, searchpath_secure=False):
  # We have to iterate over toplevel items ourselves cause the visitor does
  # breadth-first iteration, which would conflict with our search_path state
  # tracking.

  # @extschema@ is placeholder in extension scripts for
  # the schema the extension gets installed in
  sql = sql.replace("@extschema@","_extschema_")
  sql = sql.replace("@database_owner@","database_owner")
  # postgres contrib modules are protected by this to
  # prevent running extension files in psql
  sql = re.sub(r"^\\echo ","-- ",sql,flags=re.MULTILINE)

  visitor = SQLVisitor(state, searchpath_secure)
  for stmt in parse_sql(sql):
    visitor(stmt)

# PLPGSQL support is very rudimentary as the parser does not support it
# very well and bails on a few commands relevant to us eg SET/RESET
def visit_plpgsql(state, node, searchpath_secure=False):
  if not state.args.plpgsql:
    return

  match(node):
    case ast.CreateFunctionStmt():
      raw = raw_sql(node)

    # Since plpgsql parser doesnt support DO we wrap it in a procedure
    # for analysis.
    case ast.DoStmt():
      body = [b.arg.val for b in node.args if b.defname == 'as'][0]
      raw = "CREATE PROCEDURE plpgsql_do_wrapper() LANGUAGE PLPGSQL AS $wrapper$ {} $wrapper$;".format(body)

    case _:
      self.state.unknown("Unknown node in visit_plpgsql: {}".format(node))
      return

  # strip out commands currently not supported by parser
  raw = raw.replace('SET SESSION','-- SET SESSION')
  raw = raw.replace('SET LOCAL','-- SET LOCAL')
  raw = raw.replace('RESET ','-- RESET ')
  raw = raw.replace('COMMIT','-- COMMIT')
  raw = raw.replace('CALL ','SELECT ')

  parsed = parse_plpgsql(raw)

  visitor = PLPGSQLVisitor(state, searchpath_secure)
  for item in parsed:
    visitor(item)

class PLPGSQLVisitor():

  def __init__(self, state, searchpath_secure=False):
    super(self.__class__, self).__init__()
    self.searchpath_secure = searchpath_secure
    self.state = state

  def __call__(self, node):
    self.visit(node)

  def visit(self, node):
    if isinstance(node, list):
      for item in node:
        self.visit(item)
    if isinstance(node, dict):
      for key, value in node.items():
        match(key):
          case 'PLpgSQL_expr':
            visit_sql(self.state, value['query'], self.searchpath_secure)
          case _:
            self.visit(value)

class SQLVisitor(Visitor):
  def __init__(self, state, searchpath_secure=False):
    self.state = state
    self.searchpath_secure = searchpath_secure
    super(self.__class__, self).__init__()

  def visit_A_Expr(self, ancestors, node):
    if len(node.name) != 2 and not self.searchpath_secure:
      self.state.warn("Unqualified operator: {}".format(format_name(node.name)))

  def visit_CreateFunctionStmt(self, ancestors, node):
    # If the function creation is in a schema we created before we
    # consider it safe even with CREATE OR REPLACE since there would be
    # no way to precreate it.
    if len(node.funcname) == 2 and node.funcname[0].val in self.state.created_schemas:
      pass
    # This function was created without OR REPLACE previously so
    # CREATE OR REPLACE is safe now.
    elif format_function(node) in self.state.created_functions:
      pass
    elif node.replace:
      self.state.error("Unsafe function creation: {}".format(format_function(node)))

    # keep track of functions created in this script in case they get replaced later
    if node.replace == False:
      self.state.created_functions.append(format_function(node))

    # check function body
    language = [l.arg.val for l in node.options if l.defname == 'language'][0]
    body = [b.arg[0].val for b in node.options if b.defname == 'as'][0]
    setter = [s.arg for s in node.options if s.defname == 'set' and s.arg.name == "search_path"]

    if setter:
      body_secure = self.state.is_secure_searchpath(setter)
    else:
      body_secure = False

    # we allow procedures without explicit search_path here cause procedures with SET clause attached
    # cannot do transaction control
    match(language):
      case 'sql':
        if not body_secure and not node.is_procedure:
          self.state.warn("Function without explicit search_path: {}".format(format_function(node)))
        visit_sql(self.state, body, body_secure)
      case 'plpgsql':
        if not body_secure and not node.is_procedure:
          self.state.warn("Function without explicit search_path: {}".format(format_function(node)))
        visit_plpgsql(self.state, node, body_secure)
      case ('c'|'internal'):
        pass
      case _:
        self.state.unknown("Unknown function language: {}".format(language))

  def visit_CreateTransformStmt(self, ancestors, node):
    if node.replace:
      self.state.error("Unsafe transform creation: {}".format(format_name(node.type_name)))

  def visit_DefineStmt(self, ancestors, node):
    if (hasattr(node, 'replace') and node.replace) or (hasattr(node, 'if_not_exists') and node.if_not_exists):
      self.state.error("Unsafe object creation: {}".format(format_name(node.defnames)))

  def visit_VariableSetStmt(self, ancestors, node):
    # only search_path relevant
    if node.name == 'search_path':
      if node.kind == VariableSetKind.VAR_SET_VALUE:
        self.searchpath_secure = self.state.is_secure_searchpath(node)
      if node.kind == VariableSetKind.VAR_RESET:
        self.searchpath_secure = False

  def visit_AlterSeqStmt(self, ancestors, node):
    # This is not really a problem inside extension scripts since search_path
    # will be set to determined value but it might be inside function bodies.
    if not node.sequence.schemaname and not self.searchpath_secure:
      self.state.warn("Unqualified alter sequence: {}".format(node.sequence.relname))

  def visit_CaseExpr(self, ancestors, node):
    if node.arg:
      self.state.error("Unsafe CASE expression: {}".format(raw_sql(node)))

  def visit_CreateSchemaStmt(self, ancestors, node):
    if node.if_not_exists:
      self.state.error("Unsafe schema creation: {}".format(node.schemaname))
    self.state.created_schemas.append(node.schemaname)

  def visit_CreateSeqStmt(self, ancestors, node):
    if node.if_not_exists:
      self.state.error("Unsafe sequence creation: {}".format(raw_sql(node.sequence)))

  def visit_CreateStmt(self, ancestors, node):
    # We consider table creation safe even with IF NOT EXISTS if it happens in a
    # schema created in this context
    if 'schemaname' in node.relation and node.relation.schemaname in self.state.created_schemas:
      pass
    elif node.if_not_exists:
      self.state.error("Unsafe table creation: {}".format(format_name(node.relation)))

  def visit_CreateTableAsStmt(self, ancestors, node):
    if node.if_not_exists:
      self.state.error("Unsafe object creation: {}".format(format_name(node.into.rel)))

  def visit_CreateForeignServerStmt(self, ancestors, node):
    if node.if_not_exists:
      self.state.error("Unsafe foreign server creation: {}".format(node.servername))

  def visit_IndexStmt(self, ancestors, node):
    if node.if_not_exists:
      self.state.error("Unsafe index creation: {}".format(format_name(node.idxname)))

  def visit_ViewStmt(self, ancestors, node):
    if node.replace:
      self.state.error("Unsafe view creation: {}".format(format_name(node.view)))

  def visit_DoStmt(self, ancestors, node):
    language = [l.arg.val for l in node.args if l.defname == 'language']

    if language:
      language = language[0]
    else:
      language = 'plpgsql'

    match(language):
      case 'plpgsql':
        visit_plpgsql(self.state, node, self.searchpath_secure)
      case _:
        raise Exception("Unknown language: {}".format(language))

  def visit_FuncCall(self, ancestors, node):
    if len(node.funcname) != 2 and not self.searchpath_secure:
      self.state.warn("Unqualified function call: {}".format(format_name(node.funcname)))

  def visit_RangeVar(self, ancestors, node):
    if not node.schemaname and not self.searchpath_secure:
      self.state.warn("Unqualified object reference: {}".format(node.relname))

