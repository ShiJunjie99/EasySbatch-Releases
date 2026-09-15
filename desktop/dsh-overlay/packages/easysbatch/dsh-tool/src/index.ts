/** Restricted model tools for the Beta EasySbatch desktop application. */

import { spawn } from 'node:child_process'
import { isAbsolute, join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import type {} from '@deepseek-ai/dsh-agent'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { snapshotJsonValue, type JsonValue } from '@deepseek-ai/dsh-util-values'

export const name = 'beta-easysbatch-tool'
export const inject = ['tools']

const PROTOCOL_VERSION = 1
const MAX_OUTPUT_BYTES = 1024 * 1024
const TIMEOUT_MS = 75_000
let requestSequence = 0

interface CoreResponse {
  readonly protocol_version: number
  readonly id: number
  readonly result?: unknown
  readonly error?: {
    readonly code?: unknown
    readonly message?: unknown
  }
}

function coreLaunch(): { command: string; args: string[] } {
  const command = process.env.BETA_EASYSBATCH_CORE_BINARY
  if (command === undefined || command === '' || !isAbsolute(command)) {
    throw new Error('Beta EasySbatch core is not configured with an absolute executable path')
  }
  const encodedArgs = process.env.BETA_EASYSBATCH_CORE_ARGS_JSON
  if (encodedArgs === undefined || encodedArgs === '') return { command, args: [] }
  let parsed: unknown
  try {
    parsed = JSON.parse(encodedArgs)
  } catch {
    throw new Error('Beta EasySbatch core arguments are invalid')
  }
  if (!Array.isArray(parsed) || parsed.some(value => typeof value !== 'string')) {
    throw new Error('Beta EasySbatch core arguments must be a JSON string array')
  }
  return { command, args: parsed }
}

function coreEnvironment(): NodeJS.ProcessEnv {
  const allowed = new Set([
    'COMSPEC', 'HOME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'PATH', 'PATHEXT',
    'SSH_AUTH_SOCK', 'SYSTEMROOT', 'TEMP', 'TMP', 'TMPDIR', 'USERPROFILE', 'WINDIR',
  ])
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => allowed.has(key.toUpperCase())))
}

export function callCore(method: string, params: Record<string, unknown>, signal: AbortSignal): Promise<JsonValue> {
  const { command, args } = coreLaunch()
  const id = ++requestSequence
  const request = `${JSON.stringify({ protocol_version: PROTOCOL_VERSION, id, method, params })}\n`
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      env: coreEnvironment(),
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })
    const stdout: Buffer[] = []
    let stdoutBytes = 0
    let stderrBytes = 0
    let settled = false
    const finish = (callback: () => void): void => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', abort)
      callback()
    }
    const abort = (): void => {
      child.kill()
      finish(() => { reject(new Error('Beta EasySbatch core call was cancelled')) })
    }
    const timer = setTimeout(() => {
      child.kill()
      finish(() => { reject(new Error('Beta EasySbatch core call timed out')) })
    }, TIMEOUT_MS)
    timer.unref()
    if (signal.aborted) {
      abort()
      return
    }
    signal.addEventListener('abort', abort, { once: true })
    child.once('error', (error) => {
      finish(() => { reject(new Error(`Beta EasySbatch core could not start: ${error.message}`)) })
    })
    child.stdout.on('data', (chunk: Buffer) => {
      stdoutBytes += chunk.length
      if (stdoutBytes > MAX_OUTPUT_BYTES) {
        child.kill()
        finish(() => { reject(new Error('Beta EasySbatch core response exceeded the size limit')) })
        return
      }
      stdout.push(chunk)
    })
    child.stderr.on('data', (chunk: Buffer) => {
      stderrBytes += chunk.length
      if (stderrBytes > 64 * 1024) child.kill()
    })
    child.once('close', (code) => {
      finish(() => {
        if (code !== 0) {
          reject(new Error(`Beta EasySbatch core exited with status ${String(code)}`))
          return
        }
        const text = Buffer.concat(stdout).toString('utf8')
        const lines = text.split('\n').filter(line => line !== '')
        if (lines.length !== 1) {
          reject(new Error('Beta EasySbatch core returned an invalid framed response'))
          return
        }
        let response: CoreResponse
        try {
          response = JSON.parse(lines[0]!) as CoreResponse
        } catch {
          reject(new Error('Beta EasySbatch core returned invalid JSON'))
          return
        }
        if (response.protocol_version !== PROTOCOL_VERSION || response.id !== id) {
          reject(new Error('Beta EasySbatch core returned a mismatched response'))
          return
        }
        if (response.error !== undefined) {
          const codeValue = typeof response.error.code === 'string' ? response.error.code : 'CORE_ERROR'
          const message = typeof response.error.message === 'string' ? response.error.message : 'Core operation failed'
          reject(new Error(`${codeValue}: ${message}`))
          return
        }
        const result = snapshotJsonValue(response.result) as JsonValue | undefined
        if (result === undefined) {
          reject(new Error('Beta EasySbatch core returned no lossless JSON result'))
          return
        }
        resolve(result)
      })
    })
    child.stdin.once('error', () => undefined)
    child.stdin.end(request)
  })
}

export interface ProductPaths {
  readonly profilesPath: string
  readonly clusterConfigPath: string
  readonly databasePath: string
  readonly submissionRoot: string
}

export function productPaths(): ProductPaths {
  const productHome = process.env.DSH_HOME
  if (productHome === undefined || productHome === '' || !isAbsolute(productHome)) {
    throw new Error('Beta EasySbatch product home is not configured')
  }
  return {
    profilesPath: process.env.BETA_EASYSBATCH_PROFILES_PATH ?? join(productHome, 'profiles.yaml'),
    clusterConfigPath: process.env.BETA_EASYSBATCH_CLUSTER_CONFIG_PATH ?? join(productHome, 'cluster.json'),
    databasePath: join(productHome, 'jobs.sqlite3'),
    submissionRoot: join(productHome, 'runs'),
  }
}

const JSON_OUTPUT = {
  schema: { type: 'json' as const },
  render: (_args: unknown, value: unknown) => [{ type: 'text' as const, text: JSON.stringify(value) }],
}

export function apply(ctx: Context): void {
  ctx.tools.register(defineTool({
    name: 'easysbatch_capabilities',
    description: 'Report the installed Beta EasySbatch core capabilities and whether job submission is enabled.',
    parameters: {},
    output: JSON_OUTPUT,
    async execute(_args, exec) {
      return await callCore('health', {}, exec.signal)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'easysbatch_cluster_summary',
    description: 'Read a fresh aggregate Slurm snapshot through the configured system-OpenSSH connection. This is read-only and never returns another user\'s job list.',
    parameters: {},
    output: JSON_OUTPUT,
    async execute(_args, exec) {
      const paths = productPaths()
      return await callCore('cluster_snapshot', {
        cluster_config_path: paths.clusterConfigPath,
      }, exec.signal)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'easysbatch_recommend_job',
    description: 'Recommend an eligible Slurm partition and registered resource shape from a fresh cluster snapshot. It never chooses a physical node, predicts wait time, or submits the job.',
    parameters: {
      job_spec: {
        type: 'object',
        required: true,
        additionalProperties: true,
        description: 'Complete JobSpec whose explicit minimum resource requirements must be preserved.',
      },
      preference: {
        type: 'string',
        enum: ['FASTEST_AVAILABLE', 'BALANCED', 'RESOURCE_EFFICIENT'],
        default: 'BALANCED',
        description: 'Ranking preference; BALANCED is the default.',
      },
    },
    output: JSON_OUTPUT,
    async execute(args, exec) {
      const paths = productPaths()
      return await callCore('recommend_job', {
        job_spec: args.job_spec,
        profiles_path: paths.profilesPath,
        cluster_config_path: paths.clusterConfigPath,
        preference: args.preference ?? 'BALANCED',
      }, exec.signal)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'easysbatch_prepare_job',
    description: 'Validate, render, and save one immutable local task draft for explicit user review in Task history. This never contacts Slurm or submits a job.',
    parameters: {
      job_spec: {
        type: 'object',
        required: true,
        additionalProperties: true,
        description: 'Complete resolved JobSpec using absolute POSIX paths on the target cluster.',
      },
      name: {
        type: 'string',
        description: 'Optional user-facing task name.',
      },
    },
    output: JSON_OUTPUT,
    async execute(args, exec) {
      const paths = productPaths()
      return await callCore('create_job', {
        job_spec: args.job_spec,
        name: args.name ?? null,
        profiles_path: paths.profilesPath,
        database_path: paths.databasePath,
        submission_root: paths.submissionRoot,
      }, exec.signal)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'easysbatch_scan_project',
    description: 'Read-only, bounded scan of this session\'s selected local workspace. The path cannot be supplied by the model.',
    parameters: {},
    output: JSON_OUTPUT,
    async execute(_args, exec) {
      const projectDir = exec.agent?.session.header.cwd
      if (projectDir === undefined) throw new Error('This session has no selected workspace')
      return await callCore('scan_project', { project_dir: projectDir }, exec.signal)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'easysbatch_validate_job',
    description: 'Validate one strict, structured EasySbatch JobSpec. This does not render, execute, or submit anything.',
    parameters: {
      job_spec: {
        type: 'object',
        required: true,
        additionalProperties: true,
        description: 'Complete JobSpec object using absolute POSIX paths for the target cluster.',
      },
    },
    output: JSON_OUTPUT,
    async execute(args, exec) {
      return await callCore('validate_job', { job_spec: args.job_spec }, exec.signal)
    },
  }))
  ctx.tools.register(defineTool({
    name: 'easysbatch_render_job',
    description: 'Validate and deterministically render one JobSpec into a Bash/sbatch preview using the administrator-supplied profile file. This never executes or submits the script.',
    parameters: {
      job_spec: {
        type: 'object',
        required: true,
        additionalProperties: true,
        description: 'Complete JobSpec object using absolute POSIX paths for the target cluster.',
      },
    },
    output: JSON_OUTPUT,
    async execute(args, exec) {
      const profilesPath = productPaths().profilesPath
      return await callCore('render_job', {
        job_spec: args.job_spec,
        profiles_path: profilesPath,
      }, exec.signal)
    },
  }))
}
