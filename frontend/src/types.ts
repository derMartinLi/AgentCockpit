export interface ModuleRule {
  name: string
  paths: string[]
}

export interface Project {
  id: string
  name: string
  repository_path: string
  default_branch: string
  environment_spec: string
  module_mapping: ModuleRule[]
  repository_profile: {
    manifests: string[]
    ecosystems: string[]
    frameworks: string[]
    detected_at?: string
  }
  verification_commands: VerificationCommand[]
  created_at: string
  updated_at: string
}

export interface VerificationCommand {
  id: string
  name: string
  kind: 'test' | 'lint' | 'typecheck' | 'build' | 'custom'
  command: string[]
  cwd: string
  auto_run: boolean
  source: 'detected' | 'user' | 'agent'
}

export interface TaskSpec {
  goal: string
  scope: string[]
  acceptance_criteria: string[]
  constraints: string[]
  decisions: string[]
  interface?: string | null
}

export interface RefinementQuestion {
  id: string
  question: string
  reason: string
  suggested_answer: string
  blocking: boolean
}

export interface Task {
  id: string
  project_id: string
  title: string
  raw_request: string
  spec: TaskSpec | null
  status: 'DRAFT' | 'READY' | 'RUNNING' | 'NEEDS_YOU' | 'REVIEW' | 'DONE' | 'FAILED' | 'ARCHIVED'
  runtime_phase: string | null
  refinement_round: number
  inspection: {
    files_scanned: number
    manifests: string[]
    relevant_files: string[]
    sample_files: string[]
    refinement?: {
      mode: 'agent' | 'local' | string
      rationale: string
      questions_required: number
    }
  } | null
  questions: RefinementQuestion[]
  answers: Array<{ question_id: string; answer: string }>
  workspace_path: string | null
  branch_name: string | null
  baseline_commit: string | null
  source_task_id?: string | null
  archived_at?: string | null
  archived_by?: string | null
  archive_ref?: string | null
  archive_commit?: string | null
  archive_operation_id?: string | null
  created_at: string
  updated_at: string
}

export interface AgentRun {
  id: string
  task_id: string
  status: string
  summary: string | null
  result: {
    status: string
    summary: string
    changes: string[]
    checks: Array<{ name: string; status: string; detail?: string }>
    known_issues: string[]
    risks?: string[]
    needs_human: boolean
    protocol_error?: string | null
  } | null
  started_at: string
  finished_at: string | null
  cancel_requested: boolean
  previous_run_id: string | null
}

export interface WorkspaceFile {
  path: string
  status: 'Created' | 'Modified' | 'Deleted' | 'Renamed'
  added: number
  deleted: number
  modules: string[]
}

export interface WorkspaceState {
  captured_at: string
  identity: {
    verified: boolean
    workspace_path: string
    git_common_dir: string
    branch: string
    head: string
    locked: boolean
    prunable: boolean
  }
  files: WorkspaceFile[]
  modules: string[]
  diff: string
}

export interface CheckResult {
  id: string
  run_id: string
  name: string
  command: string[]
  cwd: string
  status: string
  exit_code: number | null
  output_excerpt: string | null
  started_at: string
  finished_at: string | null
  failure_kind?: 'COMMAND_INVALID' | 'COMMAND_NOT_FOUND' | 'RUNNER_ERROR' | 'CHECK_FAILED' | null
  failure_message?: string | null
  suggested_action?: string | null
}

export interface HumanInput {
  id: string
  run_id: string
  question: string
  answer: string | null
  status: 'PENDING' | 'ANSWERED'
  requested_at: string
}

export interface ReviewDecision {
  id: string
  task_id: string
  run_id: string | null
  decision: 'RESUME' | 'REJECT' | 'DONE'
  reason: string | null
  actor: string
  created_at: string
}

export interface TaskReview {
  task: Task
  run: AgentRun | null
  workspace: WorkspaceState | null
  checks: CheckResult[]
  inputs: HumanInput[]
  decisions: ReviewDecision[]
}

export interface ActivityEvent {
  id: number
  timestamp: string
  task_id: string
  run_id: string | null
  type: string
  source: string
  payload: Record<string, unknown>
}

export interface CockpitWorkspaceSummary {
  files_count: number
  additions: number
  deletions: number
  modules: string[]
  recent_files: Array<{ path: string; status: string; added: number; deleted: number }>
  captured_at: string
}

export interface CockpitTaskItem {
  task: Task
  workspace: CockpitWorkspaceSummary | null
  last_activity: ActivityEvent | null
  current_activity: ActivityEvent | null
  run: {
    id: string
    status: string
    summary: string | null
    started_at: string
    finished_at: string | null
    known_issues: string[]
  } | null
  verification: {
    total: number
    passed: number
    failed: number
    status: 'passed' | 'failed' | 'not_run'
    latest_failure: CheckResult | null
  }
  attention: {
    level: 'action' | 'live' | 'blocked' | 'warning' | 'review' | 'history'
    title: string
    detail: string | null
    action: string
  }
  workspace_retained: boolean
}

export interface CockpitRisk {
  kind: 'FILE_OVERLAP' | 'MODULE_OVERLAP'
  task_ids: string[]
  task_titles: string[]
  items: string[]
  snapshot_at: string
}

export interface ProjectCockpit {
  project_id: string
  generated_at: string
  capacity: {
    limit: number
    running: number
    available: number
  }
  sections: {
    active: CockpitTaskItem[]
    needs_you: CockpitTaskItem[]
    review: CockpitTaskItem[]
    done: CockpitTaskItem[]
    archived: CockpitTaskItem[]
  }
  history: { done: number; archived: number; total: number }
  risks: CockpitRisk[]
}

export interface ArchiveOperation {
  id: string
  task_id: string
  phase: 'PREPARING' | 'SNAPSHOTTED' | 'REMOVING' | 'COMPLETED' | 'FAILED' | string
  actor: string
  original_workspace_path: string | null
  archive_ref: string | null
  archive_commit: string | null
  error: string | null
  started_at: string
  updated_at: string
  completed_at: string | null
}

export interface ProviderSettings {
  provider: string
  model: string
  base_url: string | null
  has_api_key: boolean
  updated_at: string | null
}
