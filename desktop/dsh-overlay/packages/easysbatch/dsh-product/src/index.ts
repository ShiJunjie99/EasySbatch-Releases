/** Host-side restricted product API for the Beta EasySbatch desktop panels. */

import type { Context } from '@deepseek-ai/cordis'
import { Remote, TypertRemoteService } from '@deepseek-ai/dsh-typert-protocol'
import type { JsonValue } from '@deepseek-ai/dsh-util-values'
import {
  callCore, clearSessionPassword, productPaths, rememberSessionPassword,
} from '@beta-easysbatch/dsh-tool'

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
      catalog_path: value.catalogPath,
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

  @Remote('listProfiles')
  listProfiles(signal: AbortSignal): Promise<JsonValue> {
    return callCore('list_profiles', {
      profiles_path: paths().profilesPath,
    }, signal)
  }

  @Remote('listCatalog')
  listCatalog(signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('list_catalog', {
      profiles_path: value.profilesPath,
      catalog_path: value.catalogPath,
    }, signal)
  }

  @Remote('renderJob')
  renderJob(jobSpec: JsonValue, softwareId: string | null, signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('review_job', {
      job_spec: jobSpec,
      profiles_path: value.profilesPath,
      catalog_path: value.catalogPath,
      software_id: softwareId,
    }, signal)
  }

  @Remote('createJob')
  createJob(
    jobSpec: JsonValue,
    name: string,
    reviewSha256: string,
    signal: AbortSignal,
  ): Promise<JsonValue> {
    const value = paths()
    return callCore('create_job', {
      job_spec: jobSpec,
      name,
      review_sha256: reviewSha256,
      profiles_path: value.profilesPath,
      database_path: value.databasePath,
      submission_root: value.submissionRoot,
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

  @Remote('browseRemoteDirectory')
  browseRemoteDirectory(path: string, signal: AbortSignal): Promise<JsonValue> {
    return callCore('browse_remote_directory', {
      cluster_config_path: paths().clusterConfigPath,
      path,
    }, signal)
  }

  @Remote('scanRemoteProject')
  scanRemoteProject(path: string, signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('scan_remote_project', {
      cluster_config_path: value.clusterConfigPath,
      state_database_path: value.stateDatabasePath,
      path,
    }, signal)
  }

  @Remote('recommendResourceValues')
  recommendResourceValues(
    jobSpec: JsonValue,
    softwareId: string | null,
    scanId: string | null,
    signal: AbortSignal,
  ): Promise<JsonValue> {
    const value = paths()
    return callCore('recommend_resource_values', {
      job_spec: jobSpec,
      software_id: softwareId,
      scan_id: scanId,
      profiles_path: value.profilesPath,
      catalog_path: value.catalogPath,
      state_database_path: value.stateDatabasePath,
    }, signal)
  }

  @Remote('listPreparations')
  listPreparations(limit: number, signal: AbortSignal): Promise<JsonValue> {
    return callCore('list_preparations', {
      state_database_path: paths().stateDatabasePath,
      limit,
    }, signal)
  }

  @Remote('getPreparation')
  getPreparation(preparationId: string, signal: AbortSignal): Promise<JsonValue> {
    return callCore('get_preparation', {
      state_database_path: paths().stateDatabasePath,
      preparation_id: preparationId,
    }, signal)
  }

  @Remote('finalizePreparation')
  finalizePreparation(preparationId: string, revision: number, signal: AbortSignal): Promise<JsonValue> {
    const value = paths()
    return callCore('finalize_preparation', {
      preparation_id: preparationId,
      revision,
      state_database_path: value.stateDatabasePath,
      profiles_path: value.profilesPath,
      catalog_path: value.catalogPath,
      cluster_config_path: value.clusterConfigPath,
      database_path: value.databasePath,
      submission_root: value.submissionRoot,
    }, signal)
  }

  @Remote('configureCluster')
  async configureCluster(
    id: string,
    displayName: string,
    host: string,
    sshPort: number,
    username: string,
    password: string,
    hostKey: JsonValue,
    signal: AbortSignal,
  ): Promise<JsonValue> {
    clearSessionPassword()
    const result = await callCore('connect_cluster', {
      cluster_config_path: paths().clusterConfigPath,
      profiles_path: paths().profilesPath,
      catalog_path: paths().catalogPath,
      profile: { id, display_name: displayName, host, ssh_port: sshPort },
      username,
      host_key: hostKey,
    }, signal, password)
    rememberSessionPassword(password)
    return result
  }

  @Remote('forgetCluster')
  async forgetCluster(signal: AbortSignal): Promise<JsonValue> {
    clearSessionPassword()
    return await callCore('forget_cluster', {
      cluster_config_path: paths().clusterConfigPath,
    }, signal)
  }

  @Remote('inspectSshHostKey')
  inspectSshHostKey(host: string, sshPort: number, signal: AbortSignal): Promise<JsonValue> {
    return callCore('inspect_ssh_host_key', { host, ssh_port: sshPort }, signal)
  }

  @Remote('recommendJob')
  recommendJob(
    jobSpec: JsonValue,
    preference: string,
    softwareId: string | null,
    considerAllPartitions: boolean,
    signal: AbortSignal,
  ): Promise<JsonValue> {
    const value = paths()
    return callCore('recommend_job', {
      job_spec: jobSpec,
      preference,
      profiles_path: value.profilesPath,
      catalog_path: value.catalogPath,
      cluster_config_path: value.clusterConfigPath,
      software_id: softwareId,
      consider_all_partitions: considerAllPartitions,
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
