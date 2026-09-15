/** Browser half: EasySbatch task history and cluster overview panels. */

import type { Context as ClientContext } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-api-remotes/client'
import type { HeroBrandMarkOwnerProps } from '@deepseek-ai/dsh-client-ui-conversation/client'
import type { MainPanelId } from '@deepseek-ai/dsh-client-ui-layout/client'
import type {} from '@deepseek-ai/dsh-client-ui-renderer/client'
import type { SidebarPanelIconOwnerProps } from '@deepseek-ai/dsh-client-ui-sidebar/client'
import easySbatchRemote from '@deepseek-ai/dsh-easysbatch-product/remote'
import type {} from '@deepseek-ai/dsh-easysbatch-product/remote'
import { useCallback, useEffect, useMemo, useState } from 'react'
import css from './product.module.css'

export const inject = ['remote', 'slots']

const JOBS_PANEL = 'easysbatch-jobs' as MainPanelId
const CLUSTER_PANEL = 'easysbatch-cluster' as MainPanelId

type JsonObject = Record<string, unknown>

interface RuntimeView {
  readonly clusterConfigured: boolean
  readonly profilesConfigured: boolean
  readonly submissionEnabled: boolean
  readonly clusterLabel?: string
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
  return {
    clusterConfigured: row.cluster_configured === true,
    profilesConfigured: row.profiles_configured === true,
    submissionEnabled: row.submission_enabled === true,
    ...(cluster === undefined ? {} : {
      clusterLabel: `${string(cluster.display_name, '计算集群')} · ${string(cluster.username)}`,
    }),
  }
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
  }
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

function Icon({ kind, size, active }: { kind: 'jobs' | 'cluster'; size: number; active: boolean }) {
  return kind === 'jobs' ? (
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
      <section className={css.section}>
        <h3>日志位置</h3>
        <dl className={css.paths}>
          <dt>标准输出</dt><dd><code>{job.stdoutPath ?? '使用 Slurm 默认路径'}</code></dd>
          <dt>错误输出</dt><dd><code>{job.stderrPath ?? '使用 Slurm 默认路径'}</code></dd>
        </dl>
      </section>
      {job.renderedScript !== undefined && (
        <details className={css.script}>
          <summary>查看已保存的 sbatch 脚本</summary>
          <pre>{job.renderedScript}</pre>
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
          ? `提交配置就绪：${runtime.clusterLabel ?? '计算集群'}`
          : runtime?.clusterConfigured === true
            ? '集群连接信息已保存，但缺少管理员审核的 profiles.yaml，暂不能推荐或提交。'
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

function ClusterPanel({ ctx }: { ctx: ClientContext }) {
  const [snapshot, setSnapshot] = useState<ClusterView | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [displayName, setDisplayName] = useState('学校计算集群')
  const [host, setHost] = useState('')
  const [port, setPort] = useState('22')
  const [username, setUsername] = useState('')
  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    const result = await ctx.remote.easySbatch.clusterSnapshot()
    if (result.ok) setSnapshot(clusterOf(result.value))
    else setError(result.error.message)
    setLoading(false)
  }, [ctx])
  useEffect(() => { void load() }, [])
  const configure = async () => {
    const sshPort = Number(port)
    if (!Number.isSafeInteger(sshPort)) { setError('SSH 端口必须是整数。'); return }
    setSaving(true)
    setError(null)
    const result = await ctx.remote.easySbatch.configureCluster(
      'primary', displayName, host.trim(), sshPort, username.trim(),
    )
    if (!result.ok) {
      setError(result.error.message)
      setSaving(false)
      return
    }
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
          <label><span>集群地址</span><input value={host} placeholder="cluster.example.edu" onChange={event => { setHost(event.target.value) }} /></label>
          <label><span>SSH 端口</span><input value={port} inputMode="numeric" onChange={event => { setPort(event.target.value) }} /></label>
          <label><span>Linux 用户名</span><input value={username} autoComplete="username" onChange={event => { setUsername(event.target.value) }} /></label>
          <button type="button" className={css.primary} disabled={saving || host.trim() === '' || username.trim() === ''} onClick={() => { void configure() }}>
            {saving ? '正在保存…' : '保存并测试连接'}
          </button>
        </div>
        <small>Beta 使用系统 OpenSSH 与本机 ssh-agent，不保存集群密码或私钥。首次连接前请在系统终端确认主机指纹。</small>
      </div>}
      {snapshot !== null && (
        <>
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
