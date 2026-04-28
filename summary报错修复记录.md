# 导入 PPT 后点击生成 Summary 报错修复记录

记录时间：2026-04-13

## 问题现象

导入 12 页 PPT 后，界面提示：

`已导入 12 页 PPT，并生成基础预览。你现在可以直接修改标题、要点、summary，或基于当前内容重生成 draft / design。`

但在导入项目的内容页点击“生成 summary”时，后端执行失败，报错为：

```text
RuntimeError: 当前页资料池为空，不能生成 summary
```

## 问题复现

本次复现使用的导入项目：

- `project_id`: `ba3e89a8-8d43-4e3c-99a1-a13d89dc0de4`
- 标题：`公司产品试用环境使用说明（面向一线用户）`

复现页面：

- `page_id`: `b710d38d-0480-4211-8db9-645f2f552e1b`
- 标题：`一、试用环境是什么`

复现时该页状态：

```json
{
  "page_role": "content",
  "summary_status": "failed",
  "page_corpus_digest": {},
  "page_summary_md": "# 一、试用环境是什么 ..."
}
```

可以看到这类导入页本身已经有 `page_summary_md`，但 `page_corpus_digest` 是空对象，没有建立页级资料池。

## 根因分析

根因在于导入项目和普通搜索项目共用了同一条 `page_summary_generate` 逻辑，但两者前提不同：

1. 普通搜索项目依赖“当前页资料池”来召回证据并生成 summary。
2. 导入项目在导入时已经直接根据原始 PPT 文本生成了基础 summary 和 draft/design 预览。
3. 导入项目并不会自动建立 `page_corpus_digest`，因此再次点击“生成 summary”时，会命中普通搜索流程中的硬校验并抛异常。

也就是说，前端允许导入页点击“生成 summary”，但后端没有为“无资料池的导入页重生成 summary”提供兜底路径。

## 相关代码位置

导入项目构建页面时，已经把 summary 初始化写入数据库，但没有资料池：

- [orchestrator.py](/root/.openclaw/workspace/ppt-agent-main/backend/app/services/orchestrator.py#L1904)

原始报错发生在页级 summary 生成逻辑中：

- [orchestrator.py](/root/.openclaw/workspace/ppt-agent-main/backend/app/services/orchestrator.py#L3312)

## 修复方案

修复原则：

1. 不改变普通搜索型项目的行为。
2. 仅对“导入项目 + 内容页 + 无资料池”的情况提供兜底。
3. 兜底逻辑直接使用当前页标题和要点，重新构建 summary。
4. 重建 summary 后，将 draft/design 标记为 `stale`，符合已有工作流。

本次新增了一个辅助判断函数：

- [orchestrator.py](/root/.openclaw/workspace/ppt-agent-main/backend/app/services/orchestrator.py#L2535)

并修改了 `page_summary_generate` 的空资料池分支：

- [orchestrator.py](/root/.openclaw/workspace/ppt-agent-main/backend/app/services/orchestrator.py#L3300)

修复后的逻辑是：

- 如果不是导入项目，仍然保持原有报错：`当前页资料池为空，不能生成 summary`
- 如果是导入项目，则直接调用已有的 `_build_imported_slide_summary(...)` 重建 summary
- 生成后清空 citation，保持 `summary_status=ready`
- 同时将 `draft_status` 和 `design_status` 标记为 `stale`

## 实际修改内容

### 1. 新增导入项目判断函数

```python
def _is_imported_project(self, project: Project) -> bool:
    project_metadata = project.project_metadata_json or {}
    return bool(project_metadata.get("is_imported"))
```

### 2. 修改 `_run_page_summary(...)`

原逻辑：

```python
if not page.page_corpus_digest_json.get("document_count"):
    raise RuntimeError("当前页资料池为空，不能生成 summary")
```

新逻辑：

```python
if not page.page_corpus_digest_json.get("document_count"):
    if not self._is_imported_project(project):
        raise RuntimeError("当前页资料池为空，不能生成 summary")
    fallback_summary = self._build_imported_slide_summary(
        brief.title,
        brief.content_outline_json,
        page.page_role,
    )
    page.page_summary_md = fallback_summary
    page.page_summary_citations_json = []
    page.summary_status = "ready" if fallback_summary else "failed"
    page.draft_status = "stale" if page.current_draft_version_id else "empty"
    page.design_status = "stale" if page.current_design_version_id else "empty"
    self._update_artifact_staleness(page)
    return {"citation_count": 0, "summary_length": len(page.page_summary_md), "summary_source": "imported_outline"}
```

## 验证结果

### 1. 语法检查

执行：

```bash
python3 -m py_compile /root/.openclaw/workspace/ppt-agent-main/backend/app/services/orchestrator.py
```

结果：通过。

### 2. 接口验证

重启后端后，对同一页面再次触发：

```bash
POST /api/v1/projects/ba3e89a8-8d43-4e3c-99a1-a13d89dc0de4/pages/b710d38d-0480-4211-8db9-645f2f552e1b/summary:generate
```

修复后页面状态：

```json
{
  "summary_status": "ready",
  "draft_status": "stale",
  "design_status": "stale",
  "page_corpus_digest": {},
  "page_summary_md": "# 一、试用环境是什么 ..."
}
```

说明：

- 不再报 `RuntimeError`
- 导入页在没有资料池时也可以重新生成 summary
- 下游 draft/design 正确标记为需要重生成

## 结论

这次报错的根因不是 PPT 导入失败，而是导入项目和搜索项目共用 summary 生成逻辑时，没有处理“导入页无资料池”的场景。

修复后：

- 普通项目仍要求资料池存在
- 导入项目可以直接基于当前标题和要点重建 summary
- 与导入功能文案“可直接修改 summary 并重生成”保持一致

