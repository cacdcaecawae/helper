"""网页展示的有界副本；不截断原文库、引用校验来源或模型上下文。"""
from copy import deepcopy
import json


MAX_ROW_BYTES = 8192  # 200 行 < 1.6 MiB，留出 4 MiB IPC 上限的余量


def preview_row(source):
    row = deepcopy(source)
    fields = [(row, key) for key in ('content', 'sender_name', 'forwarded_by', 'recall_notice')]
    if isinstance(row.get('quote'), dict):
        fields.extend(((row['quote'], 'reply'), (row['quote']['reference'], 'content')))
    if row.get('kind') == 'agent':
        # 轨迹同样来自实时接口，不能让 40 个完整工具结果撑爆整批响应。
        fields.extend((step, key) for step in row.get('steps', ()) for key in ('text', 'kind', 'status'))
        fields.append((row, 'status'))
    fields = [(owner, key) for owner, key in fields if isinstance(owner.get(key), str)]
    for owner, key in fields:
        if len(owner[key]) > 2000:
            owner[key] = owner[key][:1999] + '…'
            row['preview_truncated'] = True
    encoded_size = lambda value: len(json.dumps(value, ensure_ascii=False).encode('utf-8'))
    size = encoded_size(row)
    if size <= MAX_ROW_BYTES:
        return row
    row['preview_truncated'] = True
    size = encoded_size(row)
    sizes = [encoded_size(owner[key]) for owner, key in fields]
    while size > MAX_ROW_BYTES:
        # 字段的 JSON 字节差等于整行字节差；避免为长工具轨迹重复序列化整行数百次。
        candidates = (i for i, (owner, key) in enumerate(fields) if len(owner[key]) > 2)
        index = max(candidates, key=sizes.__getitem__, default=None)
        owner, key = fields[index] if index is not None else ({}, '')
        value = owner.get(key, '')
        if len(value) <= 2:
            raise ValueError('消息元数据超过展示限制')
        owner[key] = value[:len(value)//2] + '…'
        new_size = encoded_size(owner[key])
        size += new_size - sizes[index]
        sizes[index] = new_size
    return row
