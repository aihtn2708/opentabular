"""DAX expression lexer and parser (Pratt / precedence climbing).

Covers scalar and table expressions as they appear in measure definitions.
DAX *queries* (EVALUATE / DEFINE) are out of scope: the semantic query is
expressed as measures + group-by + filters instead.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import ast as A


class DaxSyntaxError(Exception):
    def __init__(self, message: str, pos: int, source: str):
        line = source.count("\n", 0, pos) + 1
        col = pos - (source.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"{message} (line {line}, column {col})")
        self.pos, self.line, self.col = pos, line, col


@dataclass
class Token:
    kind: str  # NUM STR QNAME BRACKET IDENT OP EOF
    text: str
    pos: int


_OPS3 = ()
_OPS2 = ("&&", "||", "<>", "<=", ">=", "==")
_OPS1 = "+-*/^&=<>(),{}"


def tokenize(src: str) -> list[Token]:
    toks: list[Token] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if src.startswith("//", i) or src.startswith("--", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise DaxSyntaxError("unterminated comment", i, src)
            i = j + 2
            continue
        if c == '"':
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise DaxSyntaxError("unterminated string", i, src)
                if src[j] == '"':
                    if src[j + 1 : j + 2] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            toks.append(Token("STR", "".join(buf), i))
            i = j + 1
            continue
        if c == "'":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise DaxSyntaxError("unterminated table name", i, src)
                if src[j] == "'":
                    if src[j + 1 : j + 2] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            toks.append(Token("QNAME", "".join(buf), i))
            i = j + 1
            continue
        if c == "[":
            j, buf = i + 1, []
            while True:
                if j >= n:
                    raise DaxSyntaxError("unterminated [name]", i, src)
                if src[j] == "]":
                    if src[j + 1 : j + 2] == "]":
                        buf.append("]")
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            toks.append(Token("BRACKET", "".join(buf), i))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and src[i + 1 : i + 2].isdigit()):
            j = i
            while j < n and src[j].isdigit():
                j += 1
            if j < n and src[j] == ".":
                j += 1
                while j < n and src[j].isdigit():
                    j += 1
            if j < n and src[j] in "eE":
                k = j + 1
                if k < n and src[k] in "+-":
                    k += 1
                if k < n and src[k].isdigit():
                    while k < n and src[k].isdigit():
                        k += 1
                    j = k
            toks.append(Token("NUM", src[i:j], i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_."):
                j += 1
            toks.append(Token("IDENT", src[i:j], i))
            i = j
            continue
        two = src[i : i + 2]
        if two in _OPS2:
            toks.append(Token("OP", two, i))
            i += 2
            continue
        if c in _OPS1:
            toks.append(Token("OP", c, i))
            i += 1
            continue
        raise DaxSyntaxError(f"unexpected character {c!r}", i, src)
    toks.append(Token("EOF", "", n))
    return toks


# Binding powers, from the precedence table at https://dax.guide/operators/
# (highest first): IN, ^, sign, * /, + -, &, comparisons, NOT, &&, ||
_BINARY_BP = {
    "||": 1,
    "&&": 2,
    "=": 4, "==": 4, "<>": 4, "<": 4, "<=": 4, ">": 4, ">=": 4,
    "&": 5,
    "+": 6, "-": 6,
    "*": 7, "/": 7,
    "^": 9,
    "IN": 10,
}
_NOT_BP = 3  # NOT is an operator that binds looser than comparisons
_SIGN_BP = 8  # unary sign binds tighter than * and /, looser than ^


class Parser:
    def __init__(self, src: str):
        self.src = src
        self.toks = tokenize(src)
        self.i = 0

    # -- helpers ---------------------------------------------------------
    @property
    def tok(self) -> Token:
        return self.toks[self.i]

    def _next(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def _is_op(self, text: str) -> bool:
        return self.tok.kind == "OP" and self.tok.text == text

    def _is_kw(self, word: str) -> bool:
        return self.tok.kind == "IDENT" and self.tok.text.upper() == word

    def _expect_op(self, text: str) -> None:
        if not self._is_op(text):
            got = self.tok.text or "end of expression"
            raise DaxSyntaxError(f"expected '{text}' but found '{got}'", self.tok.pos, self.src)
        self.i += 1

    def _error(self, msg: str) -> DaxSyntaxError:
        return DaxSyntaxError(msg, self.tok.pos, self.src)

    # -- grammar ---------------------------------------------------------
    def parse(self) -> A.Node:
        if self._is_op("="):  # tolerate a leading '=' as in formula bars
            self.i += 1
        node = self.expression()
        if self.tok.kind != "EOF":
            raise self._error(f"unexpected '{self.tok.text}'")
        return node

    def expression(self) -> A.Node:
        if self._is_kw("VAR"):
            return self._var_block()
        return self._binary(0)

    def _var_block(self) -> A.Node:
        vars_: list[tuple[str, A.Node]] = []
        while self._is_kw("VAR"):
            self.i += 1
            if self.tok.kind != "IDENT":
                raise self._error("expected a variable name after VAR")
            name = self._next().text
            self._expect_op("=")
            vars_.append((name, self.expression()))
        if not self._is_kw("RETURN"):
            raise self._error("expected RETURN after VAR definitions")
        self.i += 1
        return A.VarBlock(tuple(vars_), self.expression())

    def _binary(self, min_bp: int) -> A.Node:
        left = self._unary()
        while True:
            t = self.tok
            if t.kind == "OP" and t.text in _BINARY_BP:
                op = t.text
            elif t.kind == "IDENT" and t.text.upper() == "IN":
                op = "IN"
            else:
                break
            bp = _BINARY_BP[op]
            if bp <= min_bp:
                break
            self.i += 1
            right = self._binary(bp)  # left-associative
            left = A.Binary("=" if op == "==" else op, left, right)
        return left

    def _unary(self) -> A.Node:
        t = self.tok
        if t.kind == "OP" and t.text in "+-":
            self.i += 1
            return A.Unary(t.text, self._binary(_SIGN_BP))
        if t.kind == "IDENT" and t.text.upper() == "NOT":
            self.i += 1
            return A.Unary("NOT", self._binary(_NOT_BP))
        return self._primary()

    def _primary(self) -> A.Node:
        t = self._next()
        if t.kind == "NUM":
            return A.Num(t.text)
        if t.kind == "STR":
            return A.Str(t.text)
        if t.kind == "BRACKET":
            return A.BareRef(t.text.strip())
        if t.kind == "QNAME":
            if self.tok.kind == "BRACKET":
                return A.ColumnRef(t.text, self._next().text.strip())
            return A.TableRef(t.text)
        if t.kind == "IDENT":
            upper = t.text.upper()
            if self._is_op("("):
                self.i += 1
                args: list[A.Node] = []
                if not self._is_op(")"):
                    while True:
                        if self._is_op(",") or self._is_op(")"):
                            args.append(A.Call("BLANK", ()))  # omitted argument: F(a, , c)
                        else:
                            args.append(self.expression())
                        if self._is_op(","):
                            self.i += 1
                            continue
                        break
                self._expect_op(")")
                return A.Call(upper, tuple(args))
            if self.tok.kind == "BRACKET":
                return A.ColumnRef(t.text, self._next().text.strip())
            if upper == "TRUE":
                return A.Bool(True)
            if upper == "FALSE":
                return A.Bool(False)
            if upper in ("VAR", "RETURN"):
                self.i -= 1
                raise self._error(f"unexpected {upper}")
            return A.NameRef(t.text)
        if t.kind == "OP" and t.text == "(":
            node = self.expression()
            self._expect_op(")")
            return node
        if t.kind == "OP" and t.text == "{":
            items: list[A.Node] = []
            if not self._is_op("}"):
                while True:
                    items.append(self.expression())
                    if self._is_op(","):
                        self.i += 1
                        continue
                    break
            self._expect_op("}")
            return A.SetLiteral(tuple(items))
        self.i -= 1
        raise self._error(
            f"unexpected '{t.text}'" if t.kind != "EOF" else "unexpected end of expression"
        )


def parse_dax(src: str) -> A.Node:
    """Parse a DAX expression into an AST."""
    return Parser(src).parse()
