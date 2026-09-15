/** Browser half: EasySbatch task history and cluster overview panels. */

import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-api-remotes/client'
import type { HeroBrandMarkOwnerProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { MainPanelId } from '@deepseek-ai/dsh-client-ui-layout/client'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type { SidebarPanelIconOwnerProps } from '@deepseek-ai/dsh-client-ui-sidebar/client'
import type { JsonValue } from '@deepseek-ai/dsh-util-values'
import easySbatchRemote from '@deepseek-ai/dsh-easysbatch-product/remote'
import type {} from '@deepseek-ai/dsh-easysbatch-product/remote'
import { useCallback, useEffect, useMemo, useState } from 'react'
import css from './product.module.css'

export const inject = ['remote', 'slots']

const JOBS_PANEL = 'easysbatch-jobs' as MainPanelId
const CLUSTER_PANEL = 'easysbatch-cluster' as MainPanelId
const NEW_TASK_PANEL = 'easysbatch-new-task' as MainPanelId
const PREPARATIONS_PANEL = 'easysbatch-preparations' as MainPanelId

type JsonObject = Record<string, unknown>

interface RuntimeView {
  readonly clusterConfigured: boolean
  readonly profilesConfigured: boolean
  readonly submissionEnabled: boolean
  readonly catalogConfigured: boolean
  readonly profileSource: string | null
  readonly clusterLabel?: string
  readonly clusterHost?: string
  readonly clusterPort?: number
  readonly clusterUsername?: string
  readonly trustedFingerprint?: string
}

interface JobView {
  readonly id: string
  readonly name: string
  readonly entrypoint: string
  readonly workDir: string
  readonly slurmJobId: string | null
  readonly submissionState: string
  readonly status: string | null
  readonly statusReason: string | null
  readonly createdAt: string
  readonly updatedAt: string
  readonly resources: JsonObject
  readonly stdoutPath: string | null
  readonly stderrPath: string | null
  readonly renderedScript?: string
  readonly jobSpec?: JsonObject
  readonly scriptPath?: string
  readonly submissionError?: string
}

interface ProfileView {
  readonly id: string
  readonly version: string
}

interface SoftwareView {
  readonly id: string
  readonly displayName: string
  readonly version: string | null
  readonly executable: string
  readonly runType: 'python' | 'compiled' | 'installed'
  readonly environment: ProfileView | null
  readonly verificationStatus: string
}

interface RemoteEntryView {
  readonly name: string
  readonly kind: 'directory' | 'file' | 'link' | 'other' | 'unavailable'
  readonly size: number | null
}

interface RemoteDirectoryView {
  readonly path: string
  readonly entries: readonly RemoteEntryView[]
  readonly truncated: boolean
}

interface RecommendationView {
  readonly partition: string
  readonly resources: JsonObject
  readonly reasons: readonly string[]
  readonly warnings: readonly string[]
  readonly capturedAt: string
}

interface ResourceEvidenceView {
  readonly source: string
  readonly reason: string
  readonly status: 'DIRECT'
  readonly evidence_refs: readonly string[]
}

interface ResourceValueView {
  readonly value: number
  readonly evidence: ResourceEvidenceView
}

interface RemoteScanView {
  readonly id: string
  readonly projectDir: string
  readonly scannedAt: string
  readonly filesConsidered: number
  readonly filesSkipped: number
  readonly bytesRead: number
  readonly candidates: JsonObject
  readonly warnings: readonly string[]
  readonly ambiguities: readonly string[]
  readonly limitsReached: readonly string[]
}

interface PreparationView {
  readonly id: string
  readonly revision: number
  readonly state: string
  readonly name: string
  readonly projectDir: string
  readonly entrypoint: string
  readonly unresolved: readonly JsonObject[]
  readonly warnings: readonly string[]
  readonly updatedAt: string
  readonly savedRecordId: string | null
  readonly renderedScript?: string | null
  readonly jobSpec?: JsonObject
}

interface ClusterView {
  readonly name: string
  readonly user: string
  readonly capturedAt: string
  readonly totalNodes: number
  readonly idleNodes: number
  readonly totalCpus: number | null
  readonly idleCpus: number | null
  readonly runningJobs: number | null
  readonly pendingJobs: number | null
  readonly partitions: readonly JsonObject[]
  readonly warnings: readonly string[]
}

interface SSHHostKeyView {
  readonly algorithm: string
  readonly publicKey: string
  readonly fingerprint: string
}

function object(value: unknown): JsonObject {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('EasySbatch returned an invalid response')
  }
  return value as JsonObject
}

function string(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function optionalString(value: unknown): string | null {
  return typeof value === 'string' ? value : null
}

function number(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function runtimeOf(value: unknown): RuntimeView {
  const row = object(value)
  const cluster = row.cluster === null ? undefined : object(row.cluster)
  const trusted = row.trusted_host_key === null ? undefined : object(row.trusted_host_key)
  return {
    clusterConfigured: row.cluster_configured === true,
    profilesConfigured: row.profiles_configured === true,
    submissionEnabled: row.submission_enabled === true,
    catalogConfigured: row.catalog_configured === true,
    profileSource: optionalString(row.profile_source),
    ...(cluster === undefined ? {} : {
      clusterLabel: `${string(cluster.display_name, '计算集群')} · ${string(cluster.username)}`,
      clusterHost: string(cluster.host),
      clusterPort: number(cluster.ssh_port) ?? 22,
      clusterUsername: string(cluster.username),
    }),
    ...(trusted === undefined ? {} : { trustedFingerprint: string(trusted.fingerprint) }),
  }
}

function sshHostKeyOf(value: unknown): SSHHostKeyView {
  const key = object(object(value).host_key)
  const algorithm = string(key.algorithm)
  const publicKey = string(key.public_key)
  const fingerprint = string(key.fingerprint)
  if (algorithm === '' || publicKey === '' || !fingerprint.startsWith('SHA256:')) {
    throw new Error('服务器返回了无效的 SSH 公钥。')
  }
  return { algorithm, publicKey, fingerprint }
}

function profileSourceLabel(value: string | null | undefined): string {
  if (value === 'known_cluster') return '已自动套用此服务器的共享环境配置'
  if (value === 'cluster_discovery') return '使用不带猜测命令的集群默认环境；计算资源按需从 Slurm 实时读取'
  if (value === 'custom') return '正在使用自定义环境配置'
  return '环境配置已就绪'
}

function jobOf(value: unknown): JobView {
  const row = object(value)
  const status = row.status === null ? undefined : object(row.status)
  return {
    id: string(row.id),
    name: string(row.name, '未命名任务'),
    entrypoint: string(row.entrypoint),
    workDir: string(row.work_dir),
    slurmJobId: optionalString(row.slurm_job_id),
    submissionState: string(row.submission_state, 'UNKNOWN'),
    status: status === undefined ? null : optionalString(status.normalized_state),
    statusReason: status === undefined ? null : optionalString(status.reason),
    createdAt: string(row.created_at),
    updatedAt: string(row.updated_at),
    resources: object(row.resources),
    stdoutPath: optionalString(row.stdout_path),
    stderrPath: optionalString(row.stderr_path),
    ...typeof row.rendered_script === 'string' ? { renderedScript: row.rendered_script } : {},
    ...typeof row.job_spec === 'object' && row.job_spec !== null && !Array.isArray(row.job_spec)
      ? { jobSpec: object(row.job_spec) }
      : {},
    ...typeof row.script_path === 'string' ? { scriptPath: row.script_path } : {},
    ...typeof row.submission_error === 'string' ? { submissionError: row.submission_error } : {},
  }
}

function profilesOf(value: unknown): { environments: ProfileView[]; launchers: ProfileView[] } {
  const row = object(value)
  if (!Array.isArray(row.environments) || !Array.isArray(row.launchers)) {
    throw new Error('EasySbatch returned invalid environment profiles')
  }
  const profile = (item: unknown): ProfileView => {
    const value = object(item)
    return { id: string(value.id), version: string(value.version) }
  }
  return { environments: row.environments.map(profile), launchers: row.launchers.map(profile) }
}

function softwareOf(value: unknown): SoftwareView[] {
  const rows = object(value).software
  if (!Array.isArray(rows)) throw new Error('EasySbatch returned an invalid software catalog')
  return rows.map(item => {
    const row = object(item)
    const environment = row.environment_profile === null ? null : object(row.environment_profile)
    const runType = string(row.run_type)
    if (runType !== 'python' && runType !== 'compiled' && runType !== 'installed') {
      throw new Error('EasySbatch returned an invalid software run type')
    }
    return {
      id: string(row.id),
      displayName: string(row.display_name),
      version: optionalString(row.version),
      executable: string(row.executable),
      runType,
      environment: environment === null ? null : {
        id: string(environment.id), version: string(environment.version),
      },
      verificationStatus: string(row.verification_status),
    }
  })
}

function remoteDirectoryOf(value: unknown): RemoteDirectoryView {
  const row = object(value)
  if (!Array.isArray(row.entries)) throw new Error('EasySbatch returned an invalid directory listing')
  const entries = row.entries.map(item => {
    const entry = object(item)
    const kind = string(entry.kind)
    if (!['directory', 'file', 'link', 'other', 'unavailable'].includes(kind)) {
      throw new Error('EasySbatch returned an invalid directory entry')
    }
    return {
      name: string(entry.name),
      kind: kind as RemoteEntryView['kind'],
      size: number(entry.size),
    }
  })
  return { path: string(row.path), entries, truncated: row.truncated === true }
}

function recommendationOf(value: unknown): RecommendationView {
  const row = object(value)
  if (!Array.isArray(row.recommendations) || row.recommendations.length === 0) {
    const rejections = Array.isArray(row.rejections) ? row.rejections : []
    const first = rejections.length === 0 ? undefined : object(rejections[0])
    const reasons = first !== undefined && Array.isArray(first.reasons)
      ? first.reasons.filter((item): item is string => typeof item === 'string')
      : []
    throw new Error(reasons[0] ?? '当前快照中没有满足要求的资源组合。')
  }
  const result = object(row.recommendations[0])
  return {
    partition: string(result.partition),
    resources: object(result.proposed_resources),
    reasons: Array.isArray(result.reasons)
      ? result.reasons.filter((item): item is string => typeof item === 'string')
      : [],
    warnings: Array.isArray(result.warnings)
      ? result.warnings.filter((item): item is string => typeof item === 'string')
      : [],
    capturedAt: string(result.snapshot_captured_at),
  }
}

function resourceEvidenceOf(value: unknown): ResourceEvidenceView {
  const row = object(value)
  if (!Array.isArray(row.evidence_refs)) throw new Error('资源依据格式无效。')
  return {
    source: string(row.source), reason: string(row.reason), status: 'DIRECT',
    evidence_refs: row.evidence_refs.filter((item): item is string => typeof item === 'string'),
  }
}

function resourceValueOf(value: unknown): ResourceValueView {
  const row = object(value)
  const amount = number(row.value)
  if (amount === null || amount <= 0) throw new Error('资源推荐值无效。')
  return { value: amount, evidence: resourceEvidenceOf(row.evidence) }
}

function remoteScanOf(value: unknown): RemoteScanView {
  const row = object(value)
  const summary = object(row.summary)
  if (typeof row.scan_id !== 'string' || !Array.isArray(row.warnings)
      || !Array.isArray(row.ambiguities) || !Array.isArray(row.limits_reached)) {
    throw new Error('服务器项目扫描结果无效。')
  }
  return {
    id: row.scan_id, projectDir: string(row.project_dir), scannedAt: string(row.scanned_at),
    filesConsidered: number(summary.files_considered) ?? 0,
    filesSkipped: number(summary.files_skipped) ?? 0,
    bytesRead: number(summary.bytes_read) ?? 0,
    candidates: object(row.candidates),
    warnings: row.warnings.filter((item): item is string => typeof item === 'string'),
    ambiguities: row.ambiguities.filter((item): item is string => typeof item === 'string'),
    limitsReached: row.limits_reached.filter((item): item is string => typeof item === 'string'),
  }
}

function scanCandidateValues(scan: RemoteScanView, group: string): string[] {
  const rows = scan.candidates[group]
  if (!Array.isArray(rows)) return []
  return rows.map(item => string(object(item).value)).filter(value => value !== '').slice(0, 6)
}

function preparationOf(value: unknown): PreparationView {
  const row = object(value)
  if (!Array.isArray(row.unresolved) || !Array.isArray(row.warnings)) {
    throw new Error('智能草稿格式无效。')
  }
  return {
    id: string(row.id), revision: number(row.revision) ?? 0, state: string(row.state),
    name: string(row.name, '未命名智能草稿'), projectDir: string(row.project_dir),
    entrypoint: string(row.entrypoint), unresolved: row.unresolved.map(object),
    warnings: row.warnings.filter((item): item is string => typeof item === 'string'),
    updatedAt: string(row.updated_at), savedRecordId: optionalString(row.saved_record_id),
    ...(row.rendered_script === null || typeof row.rendered_script === 'string'
      ? { renderedScript: row.rendered_script as string | null } : {}),
    ...(typeof row.job_spec === 'object' && row.job_spec !== null && !Array.isArray(row.job_spec)
      ? { jobSpec: object(row.job_spec) } : {}),
  }
}

function preparationsOf(value: unknown): readonly PreparationView[] {
  const rows = object(value).preparations
  if (!Array.isArray(rows)) throw new Error('智能草稿列表无效。')
  return rows.map(preparationOf)
}

function jobsOf(value: unknown): readonly JobView[] {
  const rows = object(value).jobs
  if (!Array.isArray(rows)) throw new Error('EasySbatch returned an invalid task list')
  return rows.map(jobOf)
}

function clusterOf(value: unknown): ClusterView {
  const row = object(value)
  const summary = object(row.summary)
  const queue = row.queue === null ? undefined : object(row.queue)
  const total = queue === undefined ? undefined : object(queue.total)
  if (!Array.isArray(row.partitions) || !Array.isArray(row.warnings)) {
    throw new Error('EasySbatch returned an invalid cluster snapshot')
  }
  return {
    name: string(row.cluster_name, '计算集群'),
    user: string(row.current_user),
    capturedAt: string(row.captured_at),
    totalNodes: number(summary.total_nodes) ?? 0,
    idleNodes: number(summary.idle_nodes) ?? 0,
    totalCpus: number(summary.total_cpus),
    idleCpus: number(summary.idle_cpus),
    runningJobs: total === undefined ? null : number(total.running_jobs),
    pendingJobs: total === undefined ? null : number(total.pending_jobs),
    partitions: row.partitions.map(object),
    warnings: row.warnings.filter((item): item is string => typeof item === 'string'),
  }
}

function date(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString('zh-CN', { hour12: false })
}

function stateLabel(value: string | null): string {
  const labels: Record<string, string> = {
    SCRIPT_RENDERED: '待确认', SUBMITTING: '提交中', SUBMITTED: '已提交',
    SUBMIT_FAILED: '提交失败', SUBMISSION_UNKNOWN: '结果待核对',
    PENDING: '排队中', RUNNING: '运行中', COMPLETED: '已完成', FAILED: '失败',
    CANCELLED: '已取消', TIMEOUT: '超时', UNKNOWN: '未知',
  }
  return value === null ? '尚未查询' : labels[value] ?? value
}

function resourceText(resources: JsonObject): string {
  const nodes = number(resources.nodes) ?? 1
  const tasks = number(resources.ntasks) ?? 1
  const cpus = number(resources.cpus_per_task) ?? 1
  const gpu = resources.gpus === null ? null : object(resources.gpus)
  const gpuCount = gpu === null ? 0 : number(gpu.count) ?? 0
  return `${string(resources.partition, '—')} · ${nodes} 节点 · ${tasks} 任务 · ${cpus} CPU${gpuCount > 0 ? ` · ${gpuCount} GPU/节点` : ''}`
}

function Icon({ kind, size, active }: { kind: 'create' | 'drafts' | 'jobs' | 'cluster'; size: number; active: boolean }) {
  return kind === 'create' ? (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
      <rect x="2.5" y="2.5" width="19" height="19" rx="5" stroke="currentColor" strokeWidth="1.5" opacity={active ? 1 : .75} />
    </svg>
  ) : kind === 'drafts' ? (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M6 3h9l3 3v15H6V3Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
      <path d="M9 10h6M9 14h6M9 18h4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  ) : kind === 'jobs' ? (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M7 4h10M7 9h10M7 14h6M5 20h14a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2Z" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <circle cx="16.5" cy="16.5" r="3.5" fill={active ? 'currentColor' : 'none'} stroke="currentColor" strokeWidth="1.5" />
    </svg>
  ) : (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <rect x="3" y="4" width="18" height="6" rx="2" stroke="currentColor" strokeWidth="1.7" />
      <rect x="3" y="14" width="18" height="6" rx="2" stroke="currentColor" strokeWidth="1.7" />
      <path d="M7 7h.01M7 17h.01M11 7h6M11 17h6" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" />
    </svg>
  )
}

function BetaMark({ size, className }: HeroBrandMarkOwnerProps) {
  return <span className={className} style={{
    alignItems: 'center', background: 'linear-gradient(145deg, #2563eb, #0f766e)',
    borderRadius: Math.max(6, Math.round(size * 0.28)), color: '#fff', display: 'inline-flex',
    fontSize: Math.round(size * 0.72), fontWeight: 800, height: size, justifyContent: 'center',
    lineHeight: 1, width: size,
  }}>β</span>
}

function Metric({ label, value }: { label: string; value: string | number | null }) {
  return <div className={css.metric}><span>{label}</span><strong>{value ?? '不可用'}</strong></div>
}

interface DraftForm {
  readonly name: string
  readonly projectDir: string
  readonly workDir: string
  readonly softwareId: string
  readonly runType: 'python' | 'compiled' | 'installed'
  readonly entrypoint: string
  readonly executable: string
  readonly args: string
  readonly environmentId: string
  readonly launcherId: string
  readonly partition: string
  readonly nodes: string
  readonly ntasks: string
  readonly cpusPerTask: string
  readonly gpuCount: string
  readonly gpuType: string
  readonly memoryMode: 'cluster_default' | 'recommended' | 'explicit'
  readonly memoryMib: string
  readonly memoryEvidence: ResourceEvidenceView | null
  readonly walltimeMode: 'cluster_default' | 'recommended' | 'explicit'
  readonly walltimeSeconds: string
  readonly walltimeEvidence: ResourceEvidenceView | null
  readonly account: string
  readonly qos: string
  readonly stdout: string
  readonly stderr: string
  readonly requiredInputs: string
  readonly prepareSteps: string
}

const EMPTY_DRAFT: DraftForm = {
  name: '', projectDir: '', workDir: '', softwareId: '', runType: 'python',
  entrypoint: '', executable: 'python', args: '', environmentId: '', launcherId: '',
  partition: '', nodes: '1', ntasks: '1', cpusPerTask: '1', gpuCount: '', gpuType: '',
  memoryMode: 'cluster_default', memoryMib: '', walltimeMode: 'cluster_default',
  memoryEvidence: null, walltimeSeconds: '', walltimeEvidence: null,
  account: '', qos: '', stdout: 'logs/%j.out', stderr: 'logs/%j.err',
  requiredInputs: '', prepareSteps: '[]',
}

function positiveInteger(value: string, label: string): number {
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed) || parsed <= 0) throw new Error(`${label}必须是正整数。`)
  return parsed
}

function lines(value: string): string[] {
  return value.split(/\r?\n/).filter(item => item.length > 0)
}

function buildJobSpec(
  form: DraftForm,
  environments: readonly ProfileView[],
  launchers: readonly ProfileView[],
): JsonValue {
  if (form.name.trim() === '') throw new Error('请填写任务名称。')
  if (!form.projectDir.startsWith('/') || !form.workDir.startsWith('/')) {
    throw new Error('项目目录和工作目录必须是服务器上的绝对路径。')
  }
  if (form.entrypoint.trim() === '' || form.executable.trim() === '') {
    throw new Error('请填写入口程序和执行命令。')
  }
  if (form.partition.trim() === '') throw new Error('请选择或填写 Slurm 分区。')
  const environment = environments.find(item => item.id === form.environmentId)
  if (environment === undefined) throw new Error('请选择一个已登记的运行环境。')
  const launcher = form.launcherId === ''
    ? undefined
    : launchers.find(item => item.id === form.launcherId)
  if (form.launcherId !== '' && launcher === undefined) throw new Error('所选并行启动器不存在。')
  let prepareSteps: unknown
  try {
    prepareSteps = JSON.parse(form.prepareSteps)
  } catch {
    throw new Error('准备步骤必须是有效的 JSON 数组。')
  }
  if (!Array.isArray(prepareSteps)) throw new Error('准备步骤必须是 JSON 数组。')
  if (form.memoryMode === 'recommended' && form.memoryEvidence === null) {
    throw new Error('内存推荐缺少可追溯依据，请重新查找。')
  }
  if (form.walltimeMode === 'recommended' && form.walltimeEvidence === null) {
    throw new Error('时限推荐缺少可追溯依据，请重新查找。')
  }
  const resources: Record<string, JsonValue> = {
    partition: form.partition.trim(),
    nodes: positiveInteger(form.nodes, '节点数'),
    ntasks: positiveInteger(form.ntasks, '任务数'),
    cpus_per_task: positiveInteger(form.cpusPerTask, '每任务 CPU 数'),
    gpus: form.gpuCount.trim() === '' ? null : {
      count: positiveInteger(form.gpuCount, '每节点 GPU 数'),
      ...(form.gpuType.trim() === '' ? {} : { gpu_type: form.gpuType.trim() }),
    },
    memory_mib: form.memoryMode === 'cluster_default'
      ? null : positiveInteger(form.memoryMib, '每节点内存'),
    time_limit_seconds: form.walltimeMode === 'cluster_default'
      ? null : positiveInteger(form.walltimeSeconds, '最长运行秒数'),
    memory_policy: {
      mode: form.memoryMode,
      ...(form.memoryMode === 'recommended' ? { evidence: form.memoryEvidence as unknown as JsonValue } : {}),
    },
    walltime_policy: {
      mode: form.walltimeMode,
      ...(form.walltimeMode === 'recommended' ? { evidence: form.walltimeEvidence as unknown as JsonValue } : {}),
    },
    ...(form.account.trim() === '' ? {} : { account: form.account.trim() }),
    ...(form.qos.trim() === '' ? {} : { qos: form.qos.trim() }),
  }
  return {
    project_dir: form.projectDir.trim(),
    work_dir: form.workDir.trim(),
    run_type: form.runType,
    entrypoint: form.entrypoint.trim(),
    environment_profile: { id: environment.id, version: environment.version },
    prepare_steps: prepareSteps as JsonValue,
    run_step: {
      executable: form.executable.trim(),
      args: lines(form.args),
      ...(launcher === undefined ? {} : {
        launcher_profile: { id: launcher.id, version: launcher.version },
      }),
    },
    resources,
    evidence: [],
    unresolved: [],
    source_fingerprints: [],
    spec_version: 1,
    job_name: form.name.trim(),
    ...(form.stdout.trim() === '' ? {} : { stdout: form.stdout.trim() }),
    ...(form.stderr.trim() === '' ? {} : { stderr: form.stderr.trim() }),
    required_inputs: lines(form.requiredInputs).map(item => item.trim()),
  }
}

function NewTaskPanel({ ctx }: { ctx: ClientContext }) {
  const [runtime, setRuntime] = useState<RuntimeView | null>(null)
  const [environments, setEnvironments] = useState<readonly ProfileView[]>([])
  const [launchers, setLaunchers] = useState<readonly ProfileView[]>([])
  const [software, setSoftware] = useState<readonly SoftwareView[]>([])
  const [partitions, setPartitions] = useState<readonly string[]>([])
  const [form, setForm] = useState<DraftForm>(EMPTY_DRAFT)
  const [preview, setPreview] = useState<string | null>(null)
  const [previewRevision, setPreviewRevision] = useState<string | null>(null)
  const [reviewToken, setReviewToken] = useState<string | null>(null)
  const [created, setCreated] = useState<JobView | null>(null)
  const [browserTarget, setBrowserTarget] = useState<'projectDir' | 'workDir' | null>(null)
  const [directory, setDirectory] = useState<RemoteDirectoryView | null>(null)
  const [browseBusy, setBrowseBusy] = useState(false)
  const [recommendation, setRecommendation] = useState<RecommendationView | null>(null)
  const [remoteScan, setRemoteScan] = useState<RemoteScanView | null>(null)
  const [resourceEvidence, setResourceEvidence] = useState<{
    readonly memory?: ResourceValueView
    readonly walltime?: ResourceValueView
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const update = <K extends keyof DraftForm>(key: K, value: DraftForm[K]) => {
    setForm(current => {
      const next = { ...current, [key]: value }
      if (key === 'memoryMode' && value !== 'recommended') next.memoryEvidence = null
      if (key === 'walltimeMode' && value !== 'recommended') next.walltimeEvidence = null
      if (key === 'memoryMib' && current.memoryMode === 'recommended') {
        next.memoryMode = 'explicit'; next.memoryEvidence = null
      }
      if (key === 'walltimeSeconds' && current.walltimeMode === 'recommended') {
        next.walltimeMode = 'explicit'; next.walltimeEvidence = null
      }
      const policyIndependent = new Set<keyof DraftForm>([
        'name', 'stdout', 'stderr', 'memoryMode', 'memoryMib', 'memoryEvidence',
        'walltimeMode', 'walltimeSeconds', 'walltimeEvidence',
      ])
      if (!policyIndependent.has(key)) {
        if (current.memoryMode === 'recommended') {
          next.memoryMode = 'explicit'; next.memoryEvidence = null
        }
        if (current.walltimeMode === 'recommended') {
          next.walltimeMode = 'explicit'; next.walltimeEvidence = null
        }
      }
      return next
    })
    setCreated(null)
    setPreview(null)
    setPreviewRevision(null)
    setReviewToken(null)
    setRecommendation(null)
    setResourceEvidence(null)
    if (key === 'projectDir') setRemoteScan(null)
  }

  useEffect(() => {
    const load = async () => {
      setLoading(true)
      setError(null)
      const statusResult = await ctx.remote.easySbatch.runtimeStatus()
      if (!statusResult.ok) { setError(statusResult.error.message); setLoading(false); return }
      const status = runtimeOf(statusResult.value)
      setRuntime(status)
      if (!status.profilesConfigured) {
        setError('请先在“集群资源”中保存服务器连接，系统会同时配置可用环境。')
        setLoading(false)
        return
      }
      const [profileResult, catalogResult] = await Promise.all([
        ctx.remote.easySbatch.listProfiles(), ctx.remote.easySbatch.listCatalog(),
      ])
      if (!profileResult.ok) { setError(profileResult.error.message); setLoading(false); return }
      const profiles = profilesOf(profileResult.value)
      setEnvironments(profiles.environments)
      setLaunchers(profiles.launchers)
      if (catalogResult.ok) setSoftware(softwareOf(catalogResult.value))
      const snapshotResult = status.clusterConfigured
        ? await ctx.remote.easySbatch.clusterSnapshot()
        : null
      const availablePartitions = snapshotResult?.ok === true
        ? clusterOf(snapshotResult.value).partitions.map(item => string(item.name)).filter(Boolean)
        : []
      setPartitions(availablePartitions)
      setForm(current => ({
        ...current,
        environmentId: current.environmentId || profiles.environments[0]?.id || '',
        partition: current.partition || availablePartitions[0] || '',
      }))
      setLoading(false)
    }
    void load()
  }, [ctx])

  const selectSoftware = (identifier: string) => {
    const selected = software.find(item => item.id === identifier)
    setForm(current => ({
      ...(selected === undefined ? { ...current, softwareId: '' } : {
        ...current,
        softwareId: selected.id,
        runType: selected.runType,
        entrypoint: selected.id,
        executable: selected.executable,
        environmentId: selected.environment?.id ?? current.environmentId,
      }),
      memoryMode: current.memoryMode === 'recommended' ? 'explicit' : current.memoryMode,
      memoryEvidence: null,
      walltimeMode: current.walltimeMode === 'recommended' ? 'explicit' : current.walltimeMode,
      walltimeEvidence: null,
    }))
    setCreated(null)
    setPreview(null)
    setPreviewRevision(null)
    setReviewToken(null)
    setRecommendation(null)
    setResourceEvidence(null)
  }

  const browse = async (path: string) => {
    setBrowseBusy(true)
    setError(null)
    const result = await ctx.remote.easySbatch.browseRemoteDirectory(path)
    if (!result.ok) setError(result.error.message)
    else setDirectory(remoteDirectoryOf(result.value))
    setBrowseBusy(false)
  }
  const scanRemote = async () => {
    const path = form.projectDir.trim()
    if (!path.startsWith('/')) {
      setError('请先选择服务器项目绝对目录。')
      return
    }
    setBusy(true)
    setError(null)
    const result = await ctx.remote.easySbatch.scanRemoteProject(path)
    if (!result.ok) setError(result.error.message)
    else {
      setRemoteScan(remoteScanOf(result.value))
      setResourceEvidence(null)
      setForm(current => ({
        ...current,
        memoryMode: current.memoryMode === 'recommended' ? 'explicit' : current.memoryMode,
        memoryEvidence: null,
        walltimeMode: current.walltimeMode === 'recommended' ? 'explicit' : current.walltimeMode,
        walltimeEvidence: null,
      }))
    }
    setBusy(false)
  }
  const openBrowser = (target: 'projectDir' | 'workDir') => {
    const path = form[target].trim()
    if (!path.startsWith('/')) {
      setError('请先输入一个服务器绝对目录，再点击浏览。')
      return
    }
    setBrowserTarget(target)
    void browse(path)
  }
  const parentDirectory = directory === null
    ? null
    : directory.path.slice(0, directory.path.lastIndexOf('/')) || '/'

  const draft = () => buildJobSpec(form, environments, launchers)
  const recommend = async () => {
    setBusy(true)
    setError(null)
    setCreated(null)
    try {
      const spec = draft()
      const result = await ctx.remote.easySbatch.recommendJob(
        spec, 'BALANCED', form.softwareId || null, true,
      )
      if (!result.ok) setError(result.error.message)
      else {
        const advice = recommendationOf(result.value)
        const resources = advice.resources
        const gpus = resources.gpus === null ? null : object(resources.gpus)
        const memoryPolicy = object(resources.memory_policy)
        const walltimePolicy = object(resources.walltime_policy)
        const memoryMode = string(memoryPolicy.mode)
        const walltimeMode = string(walltimePolicy.mode)
        if (!['cluster_default', 'recommended', 'explicit'].includes(memoryMode)
            || !['cluster_default', 'recommended', 'explicit'].includes(walltimeMode)) {
          throw new Error('推荐结果包含未知的资源策略。')
        }
        setForm(current => ({
          ...current,
          partition: string(resources.partition),
          nodes: String(number(resources.nodes) ?? 1),
          ntasks: String(number(resources.ntasks) ?? 1),
          cpusPerTask: String(number(resources.cpus_per_task) ?? 1),
          gpuCount: gpus === null ? '' : String(number(gpus.count) ?? ''),
          gpuType: gpus === null ? '' : string(gpus.gpu_type),
          memoryMode: memoryMode as DraftForm['memoryMode'],
          memoryMib: resources.memory_mib === null ? '' : String(number(resources.memory_mib) ?? ''),
          memoryEvidence: memoryMode === 'recommended'
            ? resourceEvidenceOf(memoryPolicy.evidence) : null,
          walltimeMode: walltimeMode as DraftForm['walltimeMode'],
          walltimeSeconds: resources.time_limit_seconds === null ? '' : String(number(resources.time_limit_seconds) ?? ''),
          walltimeEvidence: walltimeMode === 'recommended'
            ? resourceEvidenceOf(walltimePolicy.evidence) : null,
          account: string(resources.account), qos: string(resources.qos),
        }))
        setRecommendation(advice)
        setPreview(null)
        setPreviewRevision(null)
        setReviewToken(null)
      }
    } catch (value) {
      setError(value instanceof Error ? value.message : '无法生成资源建议。')
    }
    setBusy(false)
  }
  const recommendValues = async () => {
    setBusy(true)
    setError(null)
    try {
      const spec = draft()
      const result = await ctx.remote.easySbatch.recommendResourceValues(
        spec, form.softwareId || null, remoteScan?.id ?? null,
      )
      if (!result.ok) setError(result.error.message)
      else {
        const recommendations = object(object(result.value).recommendations)
        const memory = recommendations.memory_mib === undefined
          ? undefined : resourceValueOf(recommendations.memory_mib)
        const walltime = recommendations.time_limit_seconds === undefined
          ? undefined : resourceValueOf(recommendations.time_limit_seconds)
        if (memory === undefined && walltime === undefined) {
          throw new Error('没有找到与当前命令和并行布局完全一致的已核验内存或时限依据。')
        }
        setResourceEvidence({
          ...(memory === undefined ? {} : { memory }),
          ...(walltime === undefined ? {} : { walltime }),
        })
        setForm(current => ({
          ...current,
          ...(memory === undefined ? {} : {
            memoryMode: 'recommended' as const,
            memoryMib: String(memory.value), memoryEvidence: memory.evidence,
          }),
          ...(walltime === undefined ? {} : {
            walltimeMode: 'recommended' as const,
            walltimeSeconds: String(walltime.value), walltimeEvidence: walltime.evidence,
          }),
        }))
        setPreview(null); setPreviewRevision(null); setReviewToken(null)
      }
    } catch (value) {
      setError(value instanceof Error ? value.message : '无法生成内存或时限建议。')
    }
    setBusy(false)
  }
  const render = async () => {
    setBusy(true)
    setError(null)
    setCreated(null)
    try {
      const spec = draft()
      const result = await ctx.remote.easySbatch.renderJob(spec, form.softwareId || null)
      if (!result.ok) setError(result.error.message)
      else {
        const script = string(object(result.value).script)
        setPreview(script)
        setPreviewRevision(JSON.stringify(spec))
        setReviewToken(string(object(result.value).review_sha256))
      }
    } catch (value) {
      setError(value instanceof Error ? value.message : '任务配置无效。')
    }
    setBusy(false)
  }
  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      const spec = draft()
      if (preview === null || reviewToken === null || previewRevision !== JSON.stringify(spec)) {
        throw new Error('配置已经变化，请重新生成脚本预览后再保存。')
      }
      const result = await ctx.remote.easySbatch.createJob(spec, form.name.trim(), reviewToken)
      if (!result.ok) setError(result.error.message)
      else setCreated(jobOf(result.value))
    } catch (value) {
      setError(value instanceof Error ? value.message : '无法保存任务。')
    }
    setBusy(false)
  }
  const selectedSoftware = software.find(item => item.id === form.softwareId)

  return <main className={css.panel}>
    <header className={css.header}>
      <div><span className={css.eyebrow}>手动配置</span><h1>新建任务</h1><p>先核对服务器路径与资源，再生成可审核的 sbatch 脚本。</p></div>
    </header>
    {error !== null && <div className={css.error} role="alert">{error}</div>}
    {created !== null && <div className={`${css.connection} ${css.connected}`}><span className={css.dot} />任务“{created.name}”已保存，可在“任务记录”中审核和提交。</div>}
    <div className={css.composeLayout}>
      <section className={css.formCard}>
        {loading ? <div className={css.listMessage}>正在读取服务器配置…</div> : <>
          <div className={css.formSection}><h2>任务与程序</h2><div className={css.formGrid}>
            <label className={css.span2}><span>任务名称</span><input value={form.name} onChange={event => { update('name', event.target.value) }} /></label>
            <div className={css.fieldBlock}><span>服务器项目目录 <span className={css.fieldActions}><button type="button" className={css.textButton} disabled={!runtime?.clusterConfigured || browseBusy} onClick={() => { openBrowser('projectDir') }}>浏览</button><button type="button" className={css.textButton} disabled={!runtime?.clusterConfigured || busy} onClick={() => { void scanRemote() }}>只读扫描</button></span></span><input value={form.projectDir} placeholder="/home/user/project" onChange={event => { update('projectDir', event.target.value); if (form.workDir === '') update('workDir', event.target.value) }} /></div>
            <div className={css.fieldBlock}><span>服务器工作目录 <button type="button" className={css.textButton} disabled={!runtime?.clusterConfigured || browseBusy} onClick={() => { openBrowser('workDir') }}>浏览</button></span><input value={form.workDir} placeholder="/home/user/project" onChange={event => { update('workDir', event.target.value) }} /></div>
            {directory !== null && browserTarget !== null && <div className={`${css.remoteBrowser} ${css.span2}`}>
              <div className={css.browserBar}><div><span>服务器目录</span><code>{directory.path}</code></div><div><button type="button" className={css.textButton} disabled={browseBusy || parentDirectory === '/'} onClick={() => { if (parentDirectory !== null) void browse(parentDirectory) }}>上一级</button><button type="button" className={css.secondary} onClick={() => { update(browserTarget, directory.path); setDirectory(null); setBrowserTarget(null) }}>选择当前目录</button><button type="button" className={css.textButton} onClick={() => { setDirectory(null); setBrowserTarget(null) }}>关闭</button></div></div>
              <div className={css.browserRows}>{directory.entries.map(item => item.kind === 'directory' ? <button type="button" key={item.name} disabled={browseBusy} onClick={() => { void browse(`${directory.path}/${item.name}`) }}><span>▸</span><strong>{item.name}</strong><small>文件夹</small></button> : <div key={item.name}><span>·</span><strong>{item.name}</strong><small>{item.kind === 'file' && item.size !== null ? `${item.size} B` : item.kind}</small></div>)}</div>
              {directory.truncated && <p>目录项目较多，这里只显示前 500 项。</p>}
            </div>}
            {remoteScan !== null && <div className={`${css.scanSummary} ${css.span2}`}>
              <div><strong>已扫描服务器项目</strong><span>{remoteScan.filesConsidered} 个文件 · 读取 {remoteScan.bytesRead} B · {date(remoteScan.scannedAt)}</span></div>
              {scanCandidateValues(remoteScan, 'entrypoint_candidates').length > 0 && <div className={css.candidateRow}><span>入口候选</span>{scanCandidateValues(remoteScan, 'entrypoint_candidates').map(value => <button type="button" key={value} onClick={() => { update('entrypoint', value); if (form.runType === 'python') update('args', value) }}>{value}</button>)}</div>}
              {scanCandidateValues(remoteScan, 'input_candidates').length > 0 && <div className={css.candidateRow}><span>输入候选</span>{scanCandidateValues(remoteScan, 'input_candidates').map(value => <button type="button" key={value} onClick={() => { update('requiredInputs', [...lines(form.requiredInputs), value].filter((item, index, all) => all.indexOf(item) === index).join('\n')) }}>{value}</button>)}</div>}
              {(remoteScan.warnings.length > 0 || remoteScan.ambiguities.length > 0) && <small>{remoteScan.ambiguities[0] ?? remoteScan.warnings[0]}</small>}
            </div>}
            <label><span>已登记软件（可选）</span><select value={form.softwareId} onChange={event => { selectSoftware(event.target.value) }}><option value="">自行填写命令</option>{software.map(item => <option key={item.id} value={item.id}>{item.displayName}{item.version === null ? '' : ` ${item.version}`} · {item.verificationStatus}</option>)}</select></label>
            <label><span>任务类型</span><select value={form.runType} onChange={event => { update('runType', event.target.value as DraftForm['runType']) }}><option value="python">Python</option><option value="installed">服务器已安装程序</option><option value="compiled">需要编译</option></select></label>
            {selectedSoftware !== undefined && selectedSoftware.environment === null && <div className={`${css.notice} ${css.span2}`}>此软件只登记了共享路径，运行环境尚未验证，请人工选择并核对。</div>}
            <label><span>入口程序</span><input value={form.entrypoint} placeholder="train.py" onChange={event => { update('entrypoint', event.target.value) }} /></label>
            <label><span>执行命令</span><input value={form.executable} placeholder="python" onChange={event => { update('executable', event.target.value) }} /></label>
            <label className={css.span2}><span>命令参数（每行一个，按原样传递）</span><textarea rows={4} value={form.args} onChange={event => { update('args', event.target.value) }} /></label>
            <label><span>运行环境</span><select value={form.environmentId} onChange={event => { update('environmentId', event.target.value) }}>{environments.map(item => <option key={`${item.id}-${item.version}`} value={item.id}>{item.id} / {item.version}</option>)}</select></label>
            <label><span>并行启动器</span><select value={form.launcherId} onChange={event => { update('launcherId', event.target.value) }}><option value="">不使用</option>{launchers.map(item => <option key={`${item.id}-${item.version}`} value={item.id}>{item.id} / {item.version}</option>)}</select></label>
          </div></div>
          <div className={css.formSection}><h2>计算资源</h2><div className={css.formGrid}>
            <label><span>Slurm 分区</span>{partitions.length > 0 ? <select value={form.partition} onChange={event => { update('partition', event.target.value) }}>{partitions.map(item => <option key={item}>{item}</option>)}</select> : <input value={form.partition} onChange={event => { update('partition', event.target.value) }} />}</label>
            <label><span>账号（可选）</span><input value={form.account} onChange={event => { update('account', event.target.value) }} /></label>
            <label><span>节点数</span><input inputMode="numeric" value={form.nodes} onChange={event => { update('nodes', event.target.value) }} /></label>
            <label><span>任务数</span><input inputMode="numeric" value={form.ntasks} onChange={event => { update('ntasks', event.target.value) }} /></label>
            <label><span>每任务 CPU 数</span><input inputMode="numeric" value={form.cpusPerTask} onChange={event => { update('cpusPerTask', event.target.value) }} /></label>
            <label><span>每节点 GPU 数（可选）</span><input inputMode="numeric" value={form.gpuCount} onChange={event => { update('gpuCount', event.target.value) }} /></label>
            <label><span>GPU 类型（可选）</span><input value={form.gpuType} onChange={event => { update('gpuType', event.target.value) }} /></label>
            <label><span>QoS（可选）</span><input value={form.qos} onChange={event => { update('qos', event.target.value) }} /></label>
            <label><span>内存策略</span><select value={form.memoryMode} onChange={event => { update('memoryMode', event.target.value as DraftForm['memoryMode']) }}><option value="cluster_default">使用集群默认值</option><option value="recommended" disabled={form.memoryEvidence === null}>采用有依据的推荐</option><option value="explicit">手动指定 MiB/节点</option></select></label>
            <label><span>内存 MiB/节点</span><input disabled={form.memoryMode === 'cluster_default'} inputMode="numeric" value={form.memoryMib} onChange={event => { update('memoryMib', event.target.value) }} /></label>
            <label><span>时限策略</span><select value={form.walltimeMode} onChange={event => { update('walltimeMode', event.target.value as DraftForm['walltimeMode']) }}><option value="cluster_default">使用分区默认值</option><option value="recommended" disabled={form.walltimeEvidence === null}>采用有依据的推荐</option><option value="explicit">手动指定秒数</option></select></label>
            <label><span>最长运行秒数</span><input disabled={form.walltimeMode === 'cluster_default'} inputMode="numeric" value={form.walltimeSeconds} onChange={event => { update('walltimeSeconds', event.target.value) }} /></label>
          </div>{recommendation !== null && <div className={css.recommendation}><strong>已采用节点布局建议：{recommendation.partition}</strong><span>快照 {date(recommendation.capturedAt)} · {recommendation.reasons[0] ?? '资源容量检查通过'}</span>{recommendation.warnings.length > 0 && <small>{recommendation.warnings[0]}</small>}</div>}{resourceEvidence !== null && <div className={css.recommendation}><strong>已采用可追溯的资源值</strong>{resourceEvidence.memory !== undefined && <span>内存 {resourceEvidence.memory.value} MiB/节点 · {resourceEvidence.memory.evidence.source}</span>}{resourceEvidence.walltime !== undefined && <span>时限 {resourceEvidence.walltime.value} 秒 · {resourceEvidence.walltime.evidence.source}</span>}<small>{resourceEvidence.memory?.evidence.reason ?? resourceEvidence.walltime?.evidence.reason}</small></div>}</div>
          <details className={css.advanced}><summary>输出路径、输入文件与准备步骤</summary><div className={css.formGrid}>
            <label><span>标准输出</span><input value={form.stdout} onChange={event => { update('stdout', event.target.value) }} /></label>
            <label><span>错误输出</span><input value={form.stderr} onChange={event => { update('stderr', event.target.value) }} /></label>
            <label className={css.span2}><span>必须存在的输入文件（每行一个）</span><textarea rows={3} value={form.requiredInputs} onChange={event => { update('requiredInputs', event.target.value) }} /></label>
            <label className={css.span2}><span>准备步骤（高级 JSON；编译任务必填）</span><textarea className={css.codeInput} rows={5} value={form.prepareSteps} onChange={event => { update('prepareSteps', event.target.value) }} /></label>
          </div></details>
          <div className={css.actions}><button type="button" className={css.secondary} disabled={busy || runtime?.submissionEnabled !== true || runtime.catalogConfigured !== true} onClick={() => { void recommend() }}>推荐分区与节点布局</button><button type="button" className={css.secondary} disabled={busy || runtime?.catalogConfigured !== true} onClick={() => { void recommendValues() }}>查找内存/时限依据</button><button type="button" className={css.secondary} disabled={busy} onClick={() => { void render() }}>生成脚本预览</button><button type="button" className={css.primary} disabled={busy || preview === null} onClick={() => { void save() }}>保存到任务记录</button></div>
        </>}
      </section>
      <aside className={css.previewCard}><div className={css.previewHeader}><div><span className={css.eyebrow}>审核</span><h2>sbatch 脚本</h2></div><span className={css.badge}>{preview === null ? '尚未生成' : '仅预览，未提交'}</span></div>{preview === null ? <div className={css.detailEmpty}><div className={css.emptyMark}>⌘</div><p>填写左侧配置并生成预览。保存前仍会由核心层严格校验。</p></div> : <pre>{preview}</pre>}</aside>
    </div>
    {runtime?.catalogConfigured === true && <p className={css.formFootnote}>软件选项来自当前服务器的产品目录，并保留“已验证 / 有文档记录”的状态；选择不会绕过最终校验。</p>}
  </main>
}

function JobDetail({
  job, runtime, busy, onSubmit, onRefresh,
}: {
  job: JobView | null
  runtime: RuntimeView | null
  busy: boolean
  onSubmit: (job: JobView) => void
  onRefresh: (job: JobView) => void
}) {
  if (job === null) {
    return <div className={css.detailEmpty}><div className={css.emptyMark}>✓</div><h2>选择一条任务记录</h2><p>这里会显示资源配置、脚本、状态和日志路径。</p></div>
  }
  const canSubmit = job.submissionState === 'SCRIPT_RENDERED'
  const canRefresh = job.submissionState === 'SUBMITTED'
  const spec = job.jobSpec
  const runStep = spec === undefined ? undefined : object(spec.run_step)
  const environment = spec === undefined ? undefined : object(spec.environment_profile)
  const requiredInputs = spec !== undefined && Array.isArray(spec.required_inputs)
    ? spec.required_inputs.filter((item): item is string => typeof item === 'string')
    : []
  const evidence = spec !== undefined && Array.isArray(spec.evidence) ? spec.evidence : []
  const unresolved = spec !== undefined && Array.isArray(spec.unresolved) ? spec.unresolved : []
  return (
    <article className={css.detail}>
      <div className={css.detailHeader}>
        <div>
          <span className={css.eyebrow}>任务详情</span>
          <h2>{job.name}</h2>
          <p>{job.slurmJobId === null ? '尚未分配 Slurm 作业编号' : `Slurm #${job.slurmJobId}`}</p>
        </div>
        <span className={`${css.badge} ${job.status === 'RUNNING' ? css.good : ''}`}>{stateLabel(job.status ?? job.submissionState)}</span>
      </div>
      <div className={css.infoGrid}>
        <div><span>入口程序</span><strong>{job.entrypoint}</strong></div>
        <div><span>更新时间</span><strong>{date(job.updatedAt)}</strong></div>
        <div className={css.wideInfo}><span>资源</span><strong>{resourceText(job.resources)}</strong></div>
        <div className={css.wideInfo}><span>工作目录</span><code>{job.workDir}</code></div>
      </div>
      {job.statusReason !== null && <div className={css.notice}>调度说明：{job.statusReason}</div>}
      {job.submissionError !== undefined && <div className={css.error}>提交错误：{job.submissionError}</div>}
      {spec !== undefined && runStep !== undefined && environment !== undefined && <section className={css.section}>
        <h3>运行定义</h3>
        <dl className={css.paths}>
          <dt>任务类型</dt><dd>{string(spec.run_type)}</dd>
          <dt>运行环境</dt><dd><code>{string(environment.id)} / {string(environment.version)}</code></dd>
          <dt>执行命令</dt><dd><code>{string(runStep.executable)}</code></dd>
          <dt>命令参数</dt><dd><code>{Array.isArray(runStep.args) ? JSON.stringify(runStep.args) : '[]'}</code></dd>
          <dt>输入文件</dt><dd>{requiredInputs.length === 0 ? '未声明' : requiredInputs.map(item => <code key={item} className={css.inlinePath}>{item}</code>)}</dd>
          <dt>依据 / 待定</dt><dd>{evidence.length} 条依据 · {unresolved.length} 项待核对</dd>
        </dl>
      </section>}
      <section className={css.section}>
        <h3>日志位置</h3>
        <dl className={css.paths}>
          <dt>标准输出</dt><dd><code>{job.stdoutPath ?? '使用 Slurm 默认路径'}</code></dd>
          <dt>错误输出</dt><dd><code>{job.stderrPath ?? '使用 Slurm 默认路径'}</code></dd>
          {job.scriptPath !== undefined && <><dt>脚本文件</dt><dd><code>{job.scriptPath}</code></dd></>}
        </dl>
      </section>
      {job.renderedScript !== undefined && (
        <details className={css.script}>
          <summary>查看已保存的 sbatch 脚本</summary>
          <pre>{job.renderedScript}</pre>
        </details>
      )}
      {spec !== undefined && (
        <details className={css.script}>
          <summary>查看完整 JobSpec 与证据</summary>
          <pre>{JSON.stringify(spec, null, 2)}</pre>
        </details>
      )}
      <div className={css.actions}>
        {canRefresh && <button type="button" className={css.secondary} disabled={busy} onClick={() => { onRefresh(job) }}>刷新状态</button>}
        {canSubmit && (
          <button type="button" className={css.primary} disabled={busy || runtime?.submissionEnabled !== true} onClick={() => { onSubmit(job) }}>
            {runtime?.submissionEnabled === true ? '确认并提交' : '配置集群后提交'}
          </button>
        )}
      </div>
    </article>
  )
}

function JobsPanel({ ctx }: { ctx: ClientContext }) {
  const [jobs, setJobs] = useState<readonly JobView[]>([])
  const [selected, setSelected] = useState<JobView | null>(null)
  const [runtime, setRuntime] = useState<RuntimeView | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    const [history, status] = await Promise.all([
      ctx.remote.easySbatch.listJobs(100),
      ctx.remote.easySbatch.runtimeStatus(),
    ])
    if (!history.ok) { setError(history.error.message); setLoading(false); return }
    const next = jobsOf(history.value)
    setJobs(next)
    setRuntime(status.ok ? runtimeOf(status.value) : null)
    if (selected !== null) {
      const summary = next.find(item => item.id === selected.id)
      if (summary === undefined) setSelected(null)
      else {
        const detail = await ctx.remote.easySbatch.getJob(summary.id)
        if (detail.ok) setSelected(jobOf(detail.value))
      }
    }
    setLoading(false)
  }, [ctx, selected])

  useEffect(() => { void load() }, [])

  const choose = async (job: JobView) => {
    setError(null)
    const result = await ctx.remote.easySbatch.getJob(job.id)
    if (result.ok) setSelected(jobOf(result.value))
    else setError(result.error.message)
  }

  const submit = async (job: JobView) => {
    if (!window.confirm(`确认以当前集群身份提交“${job.name}”？\n\n提交后不会自动重试，请先检查脚本、路径和资源。`)) return
    setBusy(true)
    setError(null)
    const result = await ctx.remote.easySbatch.submitJob(job.id, job.id)
    if (result.ok) setSelected(jobOf(result.value))
    else setError(result.error.message)
    setBusy(false)
    await load()
  }

  const refresh = async (job: JobView) => {
    setBusy(true)
    setError(null)
    const result = await ctx.remote.easySbatch.refreshJob(job.id)
    if (result.ok) setSelected(jobOf(result.value))
    else setError(result.error.message)
    setBusy(false)
    await load()
  }

  return (
    <main className={css.panel}>
      <header className={css.header}>
        <div><span className={css.eyebrow}>历史记录</span><h1>任务记录</h1><p>查看待审核任务、Slurm 状态和日志位置。</p></div>
        <button type="button" className={css.secondary} disabled={loading} onClick={() => { void load() }}>刷新</button>
      </header>
      <div className={`${css.connection} ${runtime?.submissionEnabled === true ? css.connected : ''}`}>
        <span className={css.dot} />
        {runtime?.submissionEnabled === true
          ? `提交配置就绪：${runtime.clusterLabel ?? '计算集群'} · ${profileSourceLabel(runtime.profileSource)}`
          : runtime?.clusterConfigured === true
            ? '集群连接信息已保存，但自动环境配置不可用，暂不能推荐或提交。'
            : '集群尚未配置：可以准备和审核任务，暂不能提交。'}
      </div>
      {error !== null && <div className={css.error} role="alert">{error}</div>}
      <div className={css.jobLayout}>
        <section className={css.jobList} aria-label="任务列表">
          {loading && jobs.length === 0 && <div className={css.listMessage}>正在读取任务记录…</div>}
          {!loading && jobs.length === 0 && <div className={css.listMessage}><strong>还没有任务</strong><span>回到任务助手，描述要运行的程序即可创建。</span></div>}
          {jobs.map(job => (
            <button key={job.id} type="button" className={`${css.jobRow} ${selected?.id === job.id ? css.selected : ''}`} onClick={() => { void choose(job) }}>
              <span className={css.jobTop}><strong>{job.name}</strong><span className={css.badge}>{stateLabel(job.status ?? job.submissionState)}</span></span>
              <span className={css.jobMeta}>{job.entrypoint}</span>
              <span className={css.jobBottom}><span>{resourceText(job.resources)}</span><time>{date(job.createdAt)}</time></span>
            </button>
          ))}
        </section>
        <JobDetail job={selected} runtime={runtime} busy={busy} onSubmit={job => { void submit(job) }} onRefresh={job => { void refresh(job) }} />
      </div>
    </main>
  )
}

function preparationStateLabel(value: string): string {
  return {
    NEEDS_INPUT: '等待补充', READY_TO_SAVE: '可保存', SAVED: '已转为任务',
  }[value] ?? value
}

function SmartDraftsPanel({ ctx }: { ctx: ClientContext }) {
  const [items, setItems] = useState<readonly PreparationView[]>([])
  const [selected, setSelected] = useState<PreparationView | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    const result = await ctx.remote.easySbatch.listPreparations(100)
    if (!result.ok) setError(result.error.message)
    else {
      const next = preparationsOf(result.value)
      setItems(next)
      if (selected !== null) {
        const current = next.find(item => item.id === selected.id)
        if (current === undefined) setSelected(null)
        else {
          const detail = await ctx.remote.easySbatch.getPreparation(current.id)
          if (detail.ok) setSelected(preparationOf(detail.value))
        }
      }
    }
    setLoading(false)
  }, [ctx, selected])

  useEffect(() => { void load() }, [])

  const choose = async (item: PreparationView) => {
    setError(null)
    const result = await ctx.remote.easySbatch.getPreparation(item.id)
    if (!result.ok) setError(result.error.message)
    else setSelected(preparationOf(result.value))
  }

  const finalize = async (item: PreparationView) => {
    if (!window.confirm(`确认保存“${item.name}”第 ${item.revision} 版？\n\n系统会先只读复核服务器项目；本操作只写入任务记录，不会提交到 Slurm。`)) return
    setBusy(true); setError(null)
    const result = await ctx.remote.easySbatch.finalizePreparation(item.id, item.revision)
    if (!result.ok) setError(result.error.message)
    else setSelected(preparationOf(object(result.value).preparation))
    setBusy(false)
    await load()
  }

  return <main className={css.panel}>
    <header className={css.header}>
      <div><span className={css.eyebrow}>可恢复准备流程</span><h1>智能草稿</h1><p>AI 的分析先保存在这里；每次修订都有版本号，最终保存必须由你确认。</p></div>
      <button type="button" className={css.secondary} disabled={loading} onClick={() => { void load() }}>刷新</button>
    </header>
    {error !== null && <div className={css.error} role="alert">{error}</div>}
    <div className={css.jobLayout}>
      <section className={css.jobList} aria-label="智能草稿列表">
        {loading && items.length === 0 && <div className={css.listMessage}>正在读取智能草稿…</div>}
        {!loading && items.length === 0 && <div className={css.listMessage}><strong>还没有智能草稿</strong><span>先在“新建任务”扫描服务器项目，再在聊天区描述要运行的任务。</span></div>}
        {items.map(item => <button key={item.id} type="button" className={`${css.jobRow} ${selected?.id === item.id ? css.selected : ''}`} onClick={() => { void choose(item) }}>
          <span className={css.jobTop}><strong>{item.name}</strong><span className={css.badge}>{preparationStateLabel(item.state)}</span></span>
          <span className={css.jobMeta}>{item.entrypoint}</span>
          <span className={css.jobBottom}><span>版本 {item.revision} · {item.unresolved.length} 项待定</span><time>{date(item.updatedAt)}</time></span>
        </button>)}
      </section>
      {selected === null ? <div className={css.detailEmpty}><div className={css.emptyMark}>✦</div><h2>选择一份智能草稿</h2><p>这里会显示待补信息、服务器扫描依据和最终脚本。</p></div> : <article className={css.detail}>
        <div className={css.detailHeader}><div><span className={css.eyebrow}>智能准备 · 版本 {selected.revision}</span><h2>{selected.name}</h2><p>{selected.projectDir}</p></div><span className={css.badge}>{preparationStateLabel(selected.state)}</span></div>
        <div className={css.infoGrid}><div><span>入口程序</span><strong>{selected.entrypoint}</strong></div><div><span>更新时间</span><strong>{date(selected.updatedAt)}</strong></div><div className={css.wideInfo}><span>状态说明</span><strong>{selected.state === 'NEEDS_INPUT' ? '请回到聊天区补充下面的信息，AI 会在同一草稿上产生新版本。' : selected.state === 'READY_TO_SAVE' ? '结构校验和脚本渲染已完成，等待你的最终检查。' : '这份草稿已经保存到任务记录，仍未提交到 Slurm。'}</strong></div></div>
        {selected.unresolved.length > 0 && <section className={css.section}><h3>需要补充</h3><ul className={css.questionList}>{selected.unresolved.map((value, index) => <li key={`${string(value.field)}-${index}`}><strong>{string(value.field)}</strong><span>{string(value.reason)}</span></li>)}</ul></section>}
        {selected.warnings.length > 0 && <details className={css.warnings}><summary>{selected.warnings.length} 项扫描或配置说明</summary><ul>{selected.warnings.map(value => <li key={value}>{value}</li>)}</ul></details>}
        {selected.renderedScript !== undefined && selected.renderedScript !== null && <details className={css.script} open><summary>审核 sbatch 脚本</summary><pre>{selected.renderedScript}</pre></details>}
        {selected.jobSpec !== undefined && <details className={css.script}><summary>查看完整 JobSpec、指纹与依据</summary><pre>{JSON.stringify(selected.jobSpec, null, 2)}</pre></details>}
        <div className={css.actions}>{selected.state === 'READY_TO_SAVE' && <button type="button" className={css.primary} disabled={busy} onClick={() => { void finalize(selected) }}>{busy ? '正在复核项目…' : '复核并保存到任务记录'}</button>}{selected.state === 'SAVED' && <span className={css.savedNote}>任务记录 ID：{selected.savedRecordId}</span>}</div>
      </article>}
    </div>
  </main>
}

function ClusterPanel({ ctx }: { ctx: ClientContext }) {
  const [snapshot, setSnapshot] = useState<ClusterView | null>(null)
  const [profileSource, setProfileSource] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [displayName, setDisplayName] = useState('学校计算集群')
  const [host, setHost] = useState('')
  const [port, setPort] = useState('3088')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [trustedEndpoint, setTrustedEndpoint] = useState<string | null>(null)
  const [trustedFingerprint, setTrustedFingerprint] = useState<string | null>(null)
  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    const [result, runtime] = await Promise.all([
      ctx.remote.easySbatch.clusterSnapshot(),
      ctx.remote.easySbatch.runtimeStatus(),
    ])
    if (result.ok) setSnapshot(clusterOf(result.value))
    else setError(result.error.message)
    if (runtime.ok) {
      const view = runtimeOf(runtime.value)
      setProfileSource(view.profileSource)
      if (view.clusterHost !== undefined && view.clusterPort !== undefined) {
        setHost(current => current === '' ? view.clusterHost! : current)
        setPort(current => current === '3088' ? String(view.clusterPort) : current)
        setUsername(current => current === '' ? view.clusterUsername ?? '' : current)
        setTrustedEndpoint(`${view.clusterHost}:${view.clusterPort}`)
        setTrustedFingerprint(view.trustedFingerprint ?? null)
      }
    }
    setLoading(false)
  }, [ctx])
  useEffect(() => { void load() }, [])
  const configure = async () => {
    const sshPort = Number(port)
    if (!Number.isSafeInteger(sshPort)) { setError('SSH 端口必须是整数。'); return }
    setSaving(true)
    setError(null)
    const inspected = await ctx.remote.easySbatch.inspectSshHostKey(host.trim(), sshPort)
    if (!inspected.ok) {
      setError(inspected.error.message)
      setSaving(false)
      return
    }
    let hostKey: SSHHostKeyView
    try {
      hostKey = sshHostKeyOf(inspected.value)
    } catch (value) {
      setError(value instanceof Error ? value.message : '无法检查服务器身份。')
      setSaving(false)
      return
    }
    const endpoint = `${host.trim()}:${sshPort}`
    const alreadyTrusted = endpoint === trustedEndpoint && hostKey.fingerprint === trustedFingerprint
    if (!alreadyTrusted) {
      const changed = endpoint === trustedEndpoint && trustedFingerprint !== null
      const accepted = window.confirm(
        `${changed ? '警告：服务器 SSH 指纹与上次不同。' : '首次连接，请确认服务器 SSH 指纹。'}\n\n`
        + `${hostKey.algorithm}\n${hostKey.fingerprint}\n\n`
        + '请与管理员提供的指纹核对；确认后才会发送用户名和密码。',
      )
      if (!accepted) { setSaving(false); return }
    }
    const result = await ctx.remote.easySbatch.configureCluster(
      'primary', displayName, host.trim(), sshPort, username.trim(), password,
      {
        algorithm: hostKey.algorithm,
        public_key: hostKey.publicKey,
        fingerprint: hostKey.fingerprint,
      },
    )
    if (!result.ok) {
      setError(result.error.message)
      setSaving(false)
      return
    }
    setProfileSource(optionalString(object(result.value).profile_source))
    setTrustedEndpoint(endpoint)
    setTrustedFingerprint(hostKey.fingerprint)
    setPassword('')
    setSaving(false)
    await load()
  }
  const metrics = useMemo(() => snapshot === null ? [] : [
    ['可见节点', snapshot.totalNodes], ['空闲节点', snapshot.idleNodes],
    ['空闲 CPU', snapshot.idleCpus], ['排队任务', snapshot.pendingJobs],
  ] as const, [snapshot])
  return (
    <main className={css.panel}>
      <header className={css.header}>
        <div><span className={css.eyebrow}>实时概览</span><h1>集群资源</h1><p>快照用于资源推荐，不承诺等待时间或指定物理节点。</p></div>
        <button type="button" className={css.secondary} disabled={loading} onClick={() => { void load() }}>{loading ? '正在刷新…' : '刷新快照'}</button>
      </header>
      {error !== null && snapshot === null && <div className={css.emptyState}>
        <div className={css.emptyMark}>↗</div>
        <h2>连接计算集群</h2>
        <p>{error}</p>
        <div className={css.connectionForm}>
          <label><span>显示名称</span><input value={displayName} onChange={event => { setDisplayName(event.target.value) }} /></label>
          <label><span>服务器 IP</span><input value={host} placeholder="10.158.132.77" onChange={event => { setHost(event.target.value) }} /></label>
          <label><span>SSH 端口</span><input value={port} inputMode="numeric" onChange={event => { setPort(event.target.value) }} /></label>
          <label><span>Linux 用户名</span><input value={username} autoComplete="username" onChange={event => { setUsername(event.target.value) }} /></label>
          <label className={css.passwordField}><span>SSH 密码</span><input type="password" value={password} autoComplete="current-password" onChange={event => { setPassword(event.target.value) }} /></label>
          <button type="button" className={css.primary} disabled={saving || host.trim() === '' || username.trim() === '' || password === ''} onClick={() => { void configure() }}>
            {saving ? '正在安全连接…' : '登录并连接'}
          </button>
        </div>
        <small>服务器 IP 为 10.158.132.77 且 SSH 端口为 3088 时，会套用已审核的共享环境；其他服务器使用安全默认环境，并从 Slurm 实时读取计算资源。密码仅保留在本次软件会话中，退出后清除。</small>
      </div>}
      {snapshot !== null && (
        <>
          <div className={`${css.connection} ${css.connected}`}><span className={css.dot} />{profileSourceLabel(profileSource)}</div>
          <div className={css.clusterTitle}><div><h2>{snapshot.name}</h2><p>{snapshot.user} · {date(snapshot.capturedAt)}</p></div><span className={`${css.badge} ${css.good}`}>已连接</span></div>
          <div className={css.metrics}>{metrics.map(([label, value]) => <Metric key={label} label={label} value={value} />)}</div>
          <section className={css.partitionSection}>
            <h2>分区</h2>
            <div className={css.partitionTable}>
              <div className={css.tableHead}><span>名称</span><span>状态</span><span>节点</span><span>空闲 CPU</span><span>运行 / 排队</span><span>最长时限</span></div>
              {snapshot.partitions.map((value, index) => {
                const summary = value.summary === null ? undefined : object(value.summary)
                const queue = value.queue === null ? undefined : object(value.queue)
                return <div className={css.tableRow} key={`${string(value.name)}-${index}`}>
                  <strong>{string(value.name, '—')}{value.is_default === true ? ' · 默认' : ''}</strong>
                  <span>{string(value.raw_state, '未知')}</span>
                  <span>{summary === undefined ? '—' : number(summary.total_nodes) ?? '—'}</span>
                  <span>{summary === undefined ? '—' : number(summary.idle_cpus) ?? '—'}</span>
                  <span>{queue === undefined ? '—' : `${number(queue.running_jobs) ?? '—'} / ${number(queue.pending_jobs) ?? '—'}`}</span>
                  <span>{string(value.max_time, '—')}</span>
                </div>
              })}
            </div>
          </section>
          {snapshot.warnings.length > 0 && <details className={css.warnings}><summary>{snapshot.warnings.length} 项信息需要核对</summary><ul>{snapshot.warnings.map(item => <li key={item}>{item}</li>)}</ul></details>}
        </>
      )}
    </main>
  )
}

export async function apply(ctx: ClientContext): Promise<() => Promise<void>> {
  const disposeRemote = await ctx.remote.$mount(easySbatchRemote)
  const disposers = [
    ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: NEW_TASK_PANEL }, () => <NewTaskPanel ctx={ctx} />)),
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
      name: 'sidebar.panellist', id: NEW_TASK_PANEL, order: 5, label: '新建任务',
    }, ({ size, active }: SidebarPanelIconOwnerProps) => <Icon kind="create" size={size} active={active} />)),
    ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: PREPARATIONS_PANEL }, () => <SmartDraftsPanel ctx={ctx} />)),
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
      name: 'sidebar.panellist', id: PREPARATIONS_PANEL, order: 8, label: '智能草稿',
    }, ({ size, active }: SidebarPanelIconOwnerProps) => <Icon kind="drafts" size={size} active={active} />)),
    ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: JOBS_PANEL }, () => <JobsPanel ctx={ctx} />)),
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
      name: 'sidebar.panellist', id: JOBS_PANEL, order: 10, label: '任务记录',
    }, ({ size, active }: SidebarPanelIconOwnerProps) => <Icon kind="jobs" size={size} active={active} />)),
    ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: CLUSTER_PANEL }, () => <ClusterPanel ctx={ctx} />)),
    ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
      name: 'sidebar.panellist', id: CLUSTER_PANEL, order: 20, label: '集群资源',
    }, ({ size, active }: SidebarPanelIconOwnerProps) => <Icon kind="cluster" size={size} active={active} />)),
    ctx.slots.inject('conversation.hero.brand.mark', () => ctx.slots.register({
      name: 'conversation.hero.brand.mark',
    }, BetaMark)),
  ]
  return async () => {
    for (const dispose of disposers.reverse()) dispose()
    await disposeRemote()
  }
}
