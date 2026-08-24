const statusLabels: Record<string, string> = {
  DRAFT: '草稿',
  READY: '可执行',
  RUNNING: '执行中',
  NEEDS_YOU: '需要介入',
  REVIEW: '待审查',
  DONE: '已完成',
  ARCHIVED: '已归档',
  FAILED: '失败',
}

export function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge status-${status.toLowerCase()}`}>{statusLabels[status] ?? status}</span>
}
