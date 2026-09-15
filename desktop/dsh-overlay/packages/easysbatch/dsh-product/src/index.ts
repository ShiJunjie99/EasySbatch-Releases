/** Host-side restricted product API for the Beta EasySbatch desktop panels. */

import type { Context } from '@deepseek-ai/cordis'
import { Remote, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import type { JsonValue } from '@deepseek-ai/dsh-util-values'
import { callCore, productPaths } from '@beta-easysbatch/dsh-tool'

declare module '@deepseek-ai/cordis' {
  interface Context {
    /** Product-owned UI gateway; it is not exposed as a model tool. */
    easySbatch: EasySbatchDesktop
  }
}

function paths() {
  return productPaths()
}

export class EasySbatchDesktop extends TypertRemoteService {
  constructor(ctx: Context) {
    super(ctx, 'easySbatch')
  }

  @Remote('runtimeStatus')
  runtimeStatus(signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('runtime_status', {
      profiles_path: value.profilesPath,
      cluster_config_path: value.clusterConfigPath,
    }, signal)
  }

  @Remote('listJobs')
  listJobs(limit: number, signal: AbortSignal): Promise<JsonValue> {
    return callCore('list_jobs', {
      database_path: paths().databasePath,
      limit,
    }, signal)
  }

  @Remote('getJob')
  getJob(recordId: string, signal: AbortSignal): Promise<JsonValue> {
    return callCore('get_job', {
      database_path: paths().databasePath,
      record_id: recordId,
    }, signal)
  }

  @Remote('clusterSnapshot')
  clusterSnapshot(signal: AbortSignal): Promise<JsonValue> {
    return callCore('cluster_snapshot', {
      cluster_config_path: paths().clusterConfigPath,
    }, signal)
  }

  @Remote('configureCluster')
  configureCluster(
    id: string,
    displayName: string,
    host: string,
    sshPort: number,
    username: string,
    signal: AbortSignal,
  ): Promise<JsonValue> {
    return callCore('configure_cluster', {
      cluster_config_path: paths().clusterConfigPath,
      profile: { id, display_name: displayName, host, ssh_port: sshPort },
      username,
    }, signal)
  }

  @Remote('recommendJob')
  recommendJob(jobSpec: JsonValue, preference: string, signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('recommend_job', {
      job_spec: jobSpec,
      preference,
      profiles_path: value.profilesPath,
      cluster_config_path: value.clusterConfigPath,
    }, signal)
  }

  @Remote('submitJob')
  submitJob(recordId: string, confirmation: string, signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('submit_job', {
      record_id: recordId,
      confirmation,
      profiles_path: value.profilesPath,
      cluster_config_path: value.clusterConfigPath,
      database_path: value.databasePath,
      submission_root: value.submissionRoot,
    }, signal)
  }

  @Remote('refreshJob')
  refreshJob(recordId: string, signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('refresh_job', {
      record_id: recordId,
      profiles_path: value.profilesPath,
      cluster_config_path: value.clusterConfigPath,
      database_path: value.databasePath,
      submission_root: value.submissionRoot,
    }, signal)
  }
}

export function apply(ctx: Context): void {
  new EasySbatchDesktop(ctx)
}
