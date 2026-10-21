"""发布门禁的静态守卫 —— 保证「先测后发」这件事不会被悄悄改回去。

为什么需要它：
  · `needs:` 指向不存在的 job，在**本地完全看不出来**，要到 tag push
    那一刻才在 GitHub 上红，而那时已经该发版了；
  · 把 `test` job 删掉/改名，YAML 依然合法，`.github/` 没人会天天看；
  · `workflow_call` 触发声明漏了，`uses:` 会直接报错 —— 但只有真跑一次
    才知道。

⚠️ 这里**不 import pyyaml**：本项目是零第三方依赖，CI 也只 `pip install
   pytest`。为一条测试引入新依赖（还得同步改 CI）成本高于收益。
   下面那个 mini 解析器只认识「我这三个 workflow 用到的结构」——
   job 名 / uses / needs / on.push.tags。遇到它读不懂的行就明确报错
   失败（而不是猜），这样配置一旦变成它不认识的写法，测试会红，
   提醒改用真解析器 —— 不会静默放过。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"


# ---------------------------------------------------------------- mini parser

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_workflow(name: str) -> dict:
    """够用就行的 YAML 子集解析器。

    只提取三类信息，其余一律跳过：
      · 顶层 `on:` 下的 `push:` 是否含 `tags:`（及内容）
      · 顶层是否声明了 `workflow_call`
      · `jobs:` 下每个 job 的 `uses:` 与 `needs:`

    返回 {"on": {...}, "jobs": {job: {"uses": str|None, "needs": [...]}}}
    """
    lines = (WF / name).read_text(encoding="utf-8").splitlines()

    on_block: dict = {}
    jobs: dict[str, dict] = {}

    # 当前所在语境
    cur_top = None          # "on" / "jobs" / 其它顶层键
    cur_on_key = None       # on 下的 push / pull_request / workflow_call ...
    cur_job = None          # jobs 下当前 job 名
    cur_job_key = None      # 当前 job 内正在读的键（uses / needs）

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ind = _indent(raw)
        line = raw.strip()

        if ind == 0:
            cur_job = None
            cur_on_key = None
            if line.startswith("on:"):
                cur_top = "on"
                # `on: push` 单行写法本项目没用到，遇到就报错，不硬猜
                rest = line[3:].strip()
                assert not rest, f"{name}: 不支持单行 on: 写法，请用块写法"
            elif line.startswith("jobs:"):
                cur_top = "jobs"
            else:
                cur_top = "other"
            continue

        if cur_top == "on":
            if ind == 2 and line.endswith(":"):
                cur_on_key = line[:-1].strip()
                on_block[cur_on_key] = {}
            elif ind == 2:
                cur_on_key = line.split(":")[0].strip()
                on_block[cur_on_key] = {}
            elif ind >= 4 and cur_on_key:
                # 只关心 push.tags
                if ":" in line:
                    k, v = line.split(":", 1)
                    k, v = k.strip(), v.strip()
                    if cur_on_key == "push" and k == "tags":
                        on_block["push"]["tags"] = _parse_inline_list(v, name)

        elif cur_top == "jobs":
            if ind == 2 and line.endswith(":"):
                cur_job = line[:-1].strip()
                jobs[cur_job] = {"uses": None, "needs": []}
                cur_job_key = None
            elif ind == 4 and cur_job and ":" in line:
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                cur_job_key = k
                if k == "uses":
                    jobs[cur_job]["uses"] = v.strip('"\'')
                elif k == "needs":
                    jobs[cur_job]["needs"] = _parse_needs(v, name)
                elif k == "runs-on":
                    cur_job_key = None
            elif ind >= 6 and cur_job and cur_job_key == "needs" and line.startswith("- "):
                # needs: 的块式列表
                jobs[cur_job]["needs"].append(line[2:].strip().strip('"\''))

    return {"on": on_block, "jobs": jobs}


def _parse_inline_list(v: str, name: str) -> list[str]:
    v = v.strip()
    if not v:
        return []
    assert v.startswith("[") and v.endswith("]"), \
        f"{name}: 只支持 `tags: [..]` 内联写法，遇到 {v!r}"
    return [x.strip().strip('"\'') for x in v[1:-1].split(",") if x.strip()]


def _parse_needs(v: str, name: str) -> list[str]:
    v = v.strip()
    if not v:
        return []
    if v.startswith("["):
        return _parse_inline_list(v, name)
    return [v.strip('"\'')]


# ------------------------------------------------------------------- 测试本身

class TestTestsWorkflowIsReusable:
    def test_声明了_workflow_call(self):
        on = parse_workflow("tests.yml")["on"]
        assert "workflow_call" in on, (
            "tests.yml 必须可被 other workflow 调用，否则发布侧无法 needs 它")

    def test_在_tag_push_时也跑(self):
        on = parse_workflow("tests.yml")["on"]
        tags = (on.get("push") or {}).get("tags") or []
        assert "v*" in tags, (
            "只监听 branches 等于「发版那个 commit 没被测过」——"
            "tag 常常打在非 main 最新提交上")


@pytest.mark.parametrize("filename", ["release-exe.yml", "docker.yml"])
class TestReleaseGatedByTests:
    def test_存在_test_job_且调用_tests_workflow(self, filename):
        jobs = parse_workflow(filename)["jobs"]
        assert "test" in jobs, f"{filename} 少了 test job —— 发布不再受测试门禁保护"
        assert jobs["test"]["uses"] == "./.github/workflows/tests.yml", (
            "必须复用 tests.yml；复制一份测试步骤进来会随时间分叉")

    def test_真正的发布_job_依赖_test(self, filename):
        jobs = parse_workflow(filename)["jobs"]
        release_jobs = [n for n in jobs if n != "test"]
        assert release_jobs, f"{filename} 里除了 test 没有别的 job？"
        for name in release_jobs:
            assert "test" in jobs[name]["needs"], (
                f"{filename}:{name} 没有 needs: [test] —— 测试挂了它照样会跑")


class TestDockerfileGates:
    """Dockerfile 的两处硬要求，改回去会静默失效。"""

    def setup_method(self):
        self.text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_selftest_失败不能被吞掉(self):
        i = self.text.find("-selftest")
        assert i > 0, "Dockerfile 里找不到 -selftest"
        line_start = self.text.rfind("\n", 0, i) + 1
        line_end = self.text.find("\n", i)
        line = self.text[line_start:line_end]
        # 只检查这一行里 -selftest **之后**的部分，避免误伤注释里的 `||`
        after = line[line.find("-selftest"):]
        assert "||" not in after, (
            "自检那行不能有 `|| echo/true` —— 那等于「测了但测不过也发布」")

    def test_healthcheck_查的是_status_json_的_mtime(self):
        # 定位**指令行**（行首就是 HEALTHCHECK），不是注释里提到的那个词
        lines = self.text.splitlines()
        idx = [i for i, l in enumerate(lines) if l.startswith("HEALTHCHECK")]
        assert idx, "Dockerfile 里没有 HEALTHCHECK 指令"
        block = "\n".join(lines[idx[0]:idx[0] + 4])
        assert "status.json" in block, (
            "要查 status.json 的 mtime；查 /data 是否可写是个永远健康的常量")
        assert "getmtime" in block, "必须比 mtime，不能只判 exists"
