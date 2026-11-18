"""核心分析器：模块内污点跟踪。

污点级别（数值越大越“脏”，汇合时取大）：
    CLEAN   —— 与敏感参数无关；
    MASKED  —— 经过认可的掩码函数处理；
    UNKNOWN —— 来自分析范围外的调用，保守处理；
    TAINTED —— 直接/间接来自敏感参数的原始值。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import AnalyzerConfig, default_config


class Taint(Enum):
    CLEAN = 0
    MASKED = 1
    UNKNOWN = 2
    TAINTED = 3

    @staticmethod
    def merge(a: "Taint", b: "Taint") -> "Taint":
        return Taint(max(a.value, b.value))


class FindingKind(Enum):
    SENSITIVE_IN_LOG = "sensitive_in_log"  # 敏感原值进入日志（违规）
    MANUAL_REVIEW = "manual_review"        # 分析范围外的调用（待人工确认）


@dataclass
class Finding:
    kind: FindingKind
    line: int
    col: int
    message: str
    evidence: List[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - 展示用
        lines = [f"[{self.kind.value}] 第{self.line}行: {self.message}"]
        lines += [f"    证据: {step}" for step in self.evidence]
        return "\n".join(lines)


#: 变量环境：变量名 -> (污点级别, 证据链)
Env = Dict[str, Tuple[Taint, Tuple[str, ...]]]


def _dotted_name(node: ast.AST) -> Optional[str]:
    """把 ``logging.info`` 这类属性链还原为点分名；非名字表达式返回 None。"""
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _matches(name: Optional[str], patterns) -> bool:
    """点分全名或最后一段命中配置即视为匹配。"""
    if not name:
        return False
    return name in patterns or name.rsplit(".", 1)[-1] in patterns


def _merge_envs(a: Env, b: Env) -> Env:
    """分支汇合：污点取高；证据保留更“脏”一侧的链路。"""
    merged: Env = dict(a)
    for name, (taint_b, ev_b) in b.items():
        if name in merged:
            taint_a, ev_a = merged[name]
            taint = Taint.merge(taint_a, taint_b)
            ev = ev_a if taint_a.value >= taint_b.value else ev_b
            if not ev:
                ev = ev_a or ev_b
            merged[name] = (taint, ev)
        else:
            merged[name] = (taint_b, ev_b)
    return merged


class _Analyzer:
    def __init__(self, config: AnalyzerConfig, source_name: str):
        self.config = config
        self.source_name = source_name
        self.findings: List[Finding] = []
        self._manual_review_seen: set[Tuple[int, str]] = set()

    # ------------------------------------------------------------------ 语句
    def analyze_stmts(self, stmts: List[ast.stmt], env: Env) -> Env:
        for stmt in stmts:
            env = self._exec_stmt(stmt, env)
        return env

    def _exec_stmt(self, stmt: ast.stmt, env: Env) -> Env:
        if isinstance(stmt, ast.Assign):
            taint, evidence = self._eval(stmt.value, env)
            for target in stmt.targets:
                self._assign(target, taint, evidence, env, stmt.lineno)
        elif isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                taint, evidence = self._eval(stmt.value, env)
                self._assign(stmt.target, taint, evidence, env, stmt.lineno)
        elif isinstance(stmt, ast.AugAssign):
            taint, evidence = self._eval(stmt.value, env)
            if isinstance(stmt.target, ast.Name):
                old_taint, old_ev = env.get(stmt.target.id, (Taint.CLEAN, ()))
                merged = Taint.merge(old_taint, taint)
                env[stmt.target.id] = (
                    merged,
                    old_ev + tuple(evidence) if merged is not Taint.CLEAN else (),
                )
        elif isinstance(stmt, ast.Expr):
            self._eval(stmt.value, env)
        elif isinstance(stmt, ast.Return):
            if stmt.value is not None:
                self._eval(stmt.value, env)
        elif isinstance(stmt, ast.If):
            self._eval(stmt.test, env)
            env_body = self.analyze_stmts(stmt.body, dict(env))
            env_else = self.analyze_stmts(stmt.orelse, dict(env))
            env = _merge_envs(env_body, env_else)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            taint, evidence = self._eval(stmt.iter, env)
            loop_env = dict(env)
            self._assign(stmt.target, taint, evidence, loop_env, stmt.lineno)
            # 循环体分析两遍，让汇合后的污点在体内稳定传播。
            for _ in range(2):
                body_env = self.analyze_stmts(stmt.body, dict(loop_env))
                loop_env = _merge_envs(loop_env, body_env)
            else_env = self.analyze_stmts(stmt.orelse, dict(loop_env))
            env = _merge_envs(env, _merge_envs(loop_env, else_env))
        elif isinstance(stmt, ast.While):
            self._eval(stmt.test, env)
            loop_env = dict(env)
            for _ in range(2):
                body_env = self.analyze_stmts(stmt.body, dict(loop_env))
                loop_env = _merge_envs(loop_env, body_env)
            else_env = self.analyze_stmts(stmt.orelse, dict(loop_env))
            env = _merge_envs(env, _merge_envs(loop_env, else_env))
        elif isinstance(stmt, ast.Try) or (
            hasattr(ast, "TryStar") and isinstance(stmt, ast.TryStar)
        ):
            env = self._exec_try(stmt, env)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._analyze_function(stmt)
        elif isinstance(stmt, ast.ClassDef):
            env = self.analyze_stmts(stmt.body, env)
        elif isinstance(stmt, (ast.IfExp,)):  # pragma: no cover - 防御
            self._eval(stmt, env)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass, ast.Break,
                               ast.Continue, ast.Global, ast.Nonlocal)):
            pass
        elif isinstance(stmt, ast.With) or isinstance(stmt, ast.AsyncWith):
            for item in stmt.items:
                taint, evidence = self._eval(item.context_expr, env)
                if item.optional_vars is not None:
                    self._assign(item.optional_vars, taint, evidence, env, stmt.lineno)
            env = self.analyze_stmts(stmt.body, env)
        elif isinstance(stmt, ast.Raise):
            if stmt.exc is not None:
                self._eval(stmt.exc, env)
        elif isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    env.pop(target.id, None)
        else:  # pragma: no cover - 其余语句类型保守忽略
            pass
        return env

    def _exec_try(self, stmt: ast.stmt, env: Env) -> Env:
        branches: List[Env] = [self.analyze_stmts(stmt.body, dict(env))]
        for handler in stmt.handlers:
            h_env = dict(env)
            if handler.name:
                h_env[handler.name] = (Taint.UNKNOWN, ())
            branches.append(self.analyze_stmts(handler.body, h_env))
        merged = env
        for branch in branches:
            merged = _merge_envs(merged, branch)
        merged = self.analyze_stmts(stmt.orelse, merged)
        merged = self.analyze_stmts(stmt.finalbody, merged)
        return merged

    def _assign(self, target: ast.AST, taint: Taint, evidence: List[str],
                env: Env, lineno: int) -> None:
        if isinstance(target, ast.Name):
            ev = tuple(evidence)
            if taint is Taint.TAINTED:
                ev = ev + (f"第{lineno}行: 敏感数据赋值给局部变量 '{target.id}'",)
            env[target.id] = (taint, ev)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._assign(elt, taint, evidence, env, lineno)
        elif isinstance(target, ast.Starred):
            self._assign(target.value, taint, evidence, env, lineno)
        elif isinstance(target, (ast.Attribute, ast.Subscript)):
            # 属性/下标写入：保守地把污点挂到基对象名上。
            base = target
            while isinstance(base, (ast.Attribute, ast.Subscript)):
                base = base.value
            if isinstance(base, ast.Name):
                old_taint, old_ev = env.get(base.id, (Taint.CLEAN, ()))
                env[base.id] = (Taint.merge(old_taint, taint), old_ev + tuple(evidence))

    # ------------------------------------------------------------------ 表达式
    def _eval(self, node: ast.AST, env: Env) -> Tuple[Taint, List[str]]:
        if isinstance(node, ast.Constant):
            return Taint.CLEAN, []
        if isinstance(node, ast.Name):
            taint, ev = env.get(node.id, (Taint.CLEAN, ()))
            return taint, list(ev)
        if isinstance(node, ast.JoinedStr):  # f-string
            return self._combine((self._eval(v, env) for v in node.values))
        if isinstance(node, ast.FormattedValue):
            return self._eval(node.value, env)
        if isinstance(node, ast.BinOp):
            (tl, el), (tr, er) = self._eval(node.left, env), self._eval(node.right, env)
            return Taint.merge(tl, tr), el + er
        if isinstance(node, ast.BoolOp):
            return self._combine((self._eval(v, env) for v in node.values))
        if isinstance(node, ast.UnaryOp):
            return self._eval(node.operand, env)
        if isinstance(node, ast.Compare):
            parts = [self._eval(node.left, env)]
            parts += [self._eval(c, env) for c in node.comparators]
            return self._combine(iter(parts))
        if isinstance(node, ast.IfExp):
            self._eval(node.test, env)
            return self._combine(iter([self._eval(node.body, env),
                                       self._eval(node.orelse, env)]))
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return self._combine((self._eval(e, env) for e in node.elts))
        if isinstance(node, ast.Dict):
            parts = [self._eval(k, env) for k in node.keys if k is not None]
            parts += [self._eval(v, env) for v in node.values]
            return self._combine(iter(parts))
        if isinstance(node, ast.Starred):
            return self._eval(node.value, env)
        if isinstance(node, ast.Attribute):
            return self._eval(node.value, env)
        if isinstance(node, ast.Subscript):
            (tv, ev), (ts, es) = self._eval(node.value, env), self._eval(node.slice, env)
            return Taint.merge(tv, ts), ev + es
        if isinstance(node, ast.Await):
            return self._eval(node.value, env)
        if isinstance(node, ast.Call):
            return self._eval_call(node, env)
        if isinstance(node, ast.Lambda):
            return Taint.CLEAN, []
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            inner = dict(env)
            for comp in node.generators:
                taint, evidence = self._eval(comp.iter, inner)
                self._assign(comp.target, taint, evidence, inner, node.lineno)
            if isinstance(node, ast.DictComp):
                return self._combine(iter([self._eval(node.key, inner),
                                           self._eval(node.value, inner)]))
            return self._eval(node.elt, inner)
        return Taint.CLEAN, []

    @staticmethod
    def _combine(parts) -> Tuple[Taint, List[str]]:
        taint, evidence = Taint.CLEAN, []
        for t, ev in parts:
            taint = Taint.merge(taint, t)
            evidence.extend(ev)
        return taint, evidence

    def _eval_call(self, node: ast.Call, env: Env) -> Tuple[Taint, List[str]]:
        name = _dotted_name(node.func)
        arg_parts = [self._eval(a, env) for a in node.args]
        arg_parts += [self._eval(k.value, env) for k in node.keywords]
        arg_taint, arg_evidence = self._combine(iter(arg_parts)) if arg_parts else (Taint.CLEAN, [])

        if _matches(name, self.config.log_functions):
            self._check_log_sink(node, name, arg_taint, arg_evidence)
            return Taint.CLEAN, []

        if _matches(name, self.config.mask_functions):
            evidence = arg_evidence + [
                f"第{node.lineno}行: 经认可的掩码函数 '{name}' 脱敏"
            ]
            return Taint.MASKED, evidence

        # str.format：格式化字符串的一种，结果携带参数污点。
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            base_taint, base_ev = self._eval(node.func.value, env)
            return (
                Taint.merge(base_taint, arg_taint),
                base_ev + arg_evidence,
            )

        if _matches(name, self.config.known_safe_functions):
            return arg_taint, arg_evidence

        # 分析范围外的调用：独立标为待人工确认，返回值保守传播污点。
        display = name or "<动态调用>"
        key = (node.lineno, display)
        if key not in self._manual_review_seen:
            self._manual_review_seen.add(key)
            self.findings.append(
                Finding(
                    kind=FindingKind.MANUAL_REVIEW,
                    line=node.lineno,
                    col=node.col_offset,
                    message=(
                        f"调用 '{display}' 不在日志/掩码/已知安全函数配置内，"
                        "需人工确认其是否会输出或泄露敏感数据"
                    ),
                    evidence=list(arg_evidence),
                )
            )
        if arg_taint is Taint.TAINTED:
            return Taint.TAINTED, arg_evidence
        if arg_taint is Taint.CLEAN:
            return Taint.CLEAN, []
        return Taint.UNKNOWN, arg_evidence

    def _check_log_sink(self, node: ast.Call, name: Optional[str],
                        arg_taint: Taint, arg_evidence: List[str]) -> None:
        if arg_taint is Taint.TAINTED:
            evidence = list(dict.fromkeys(arg_evidence))  # 去重保持顺序
            evidence.append(
                f"第{node.lineno}行: 日志函数 '{name}' 输出未脱敏的敏感数据"
            )
            self.findings.append(
                Finding(
                    kind=FindingKind.SENSITIVE_IN_LOG,
                    line=node.lineno,
                    col=node.col_offset,
                    message=(
                        f"敏感参数原值经局部变量/格式化字符串流入日志函数 '{name}'，"
                        "日志中可能出现完整邮箱或手机号"
                    ),
                    evidence=evidence,
                )
            )

    # ------------------------------------------------------------------ 函数
    def _analyze_function(self, func) -> None:
        env: Env = {}
        args = func.args
        params = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg:
            params.append(args.vararg)
        if args.kwarg:
            params.append(args.kwarg)
        for param in params:
            if param.arg in self.config.sensitive_params:
                env[param.arg] = (
                    Taint.TAINTED,
                    (f"第{func.lineno}行: 敏感参数 '{param.arg}' 进入函数 '{func.name}'",),
                )
        for default in list(args.defaults) + [d for d in args.kw_defaults if d]:
            self._eval(default, env)
        self.analyze_stmts(func.body, env)


def analyze_source(source: str, config: Optional[AnalyzerConfig] = None,
                   source_name: str = "<source>") -> List[Finding]:
    """分析一段 Python 源码，返回按行号排序的发现列表。"""
    config = config or default_config()
    tree = ast.parse(source, filename=source_name)
    analyzer = _Analyzer(config, source_name)
    analyzer.analyze_stmts(tree.body, {})
    return sorted(analyzer.findings, key=lambda f: (f.line, f.kind.value))


def analyze_file(path: str | Path, config: Optional[AnalyzerConfig] = None) -> List[Finding]:
    path = Path(path)
    return analyze_source(
        path.read_text(encoding="utf-8"), config=config, source_name=str(path)
    )
