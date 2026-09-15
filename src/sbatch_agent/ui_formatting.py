"""Pure UI formats. Domain durations remain seconds and arguments remain arrays."""

import json
import re
import shlex


FIELD_LABELS = {
    'software_id': '运行软件', 'name': '任务名称', 'job_name': '任务名称',
    'project_dir': '项目目录', 'work_dir': '工作目录', 'run_type': '运行方式',
    'entrypoint': '入口程序', 'executable': '执行程序', 'args': '程序参数',
    'required_inputs': '输入文件', 'environment_profile': '运行环境',
    'environment_requirements': '环境需求', 'prepare_steps': '准备步骤',
    'launcher_profile': '启动配置', 'partition': '分区', 'account': '计费账户',
    'qos': '服务等级', 'nodes': '节点数', 'ntasks': '进程数',
    'cpus_per_task': '每进程 CPU 数', 'gpu_count': '每节点 GPU 数', 'gpus': 'GPU 配置',
    'gpu_type': 'GPU 型号', 'memory_mib': '每节点内存', 'time_limit_seconds': '运行时限',
    'stdout': '标准输出路径', 'stderr': '错误输出路径', 'spec_version': '配置修订号',
    'evidence': '字段依据', 'source_fingerprints': '文件指纹', 'unresolved': '待确认项',
    'resources': '计算资源', 'run_step': '运行命令', 'parallelism': '并行方式',
    'threads': '线程并行', 'mpi': '多进程并行', 'gpu': 'GPU 使用',
    'serial': '串行运行', 'python_version': 'Python 版本', 'packages': '依赖包',
    'software': '软件需求',
}


def field_label(key):
    parts = str(key).split('.')
    return ' · '.join(FIELD_LABELS.get(p, '配置项') for p in parts if not p.isdigit()) or '任务配置'


def format_duration(value):
    if value is None or value == '':
        return ''
    seconds = int(value)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d}'


def parse_duration(value):
    # Keep existing integer-second clients compatible; the UI uses HH:MM:SS.
    if re.fullmatch(r'[0-9]{1,12}', value):
        return int(value)
    match = re.fullmatch(r'([0-9]{2,8}):([0-5][0-9]):([0-5][0-9])', value)
    if match:
        hours, minutes, seconds = map(int, match.groups())
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError('运行时限请按 HH:MM:SS 填写，例如 02:00:00。')


def format_args(value):
    return shlex.join(value) if value else ''


def parse_args(value):
    """Tokenize quoted arguments only; never evaluate/expand a command or shell."""
    try:
        if value.lstrip().startswith(('[', '{')):
            items = json.loads(value)
            if not isinstance(items, list):
                raise ValueError('程序参数必须是 JSON 数组或带引号的参数文本。')
        else:
            items = shlex.split(value, comments=False, posix=True)
        if not all(isinstance(item, str) for item in items):
            raise ValueError('程序参数的每一项必须是文本。')
        for item in items:
            item.encode('utf-8')
            if '\x00' in item:
                raise ValueError('程序参数不能包含空字符。')
        return items
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('程序参数格式不正确，请检查引号或 JSON 数组；例如 --input "case 01.json"。') from exc


def display_form(form):
    """Format valid values; retain invalid user input verbatim for correction."""
    result = dict(form)
    for key, parser, formatter in [('time_limit_seconds', parse_duration, format_duration),
                                   ('args', parse_args, format_args)]:
        if result.get(key):
            try:
                result[key] = formatter(parser(result[key]))
            except ValueError:
                pass
    return result
