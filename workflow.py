"""Reviewed movie operations, generated NFOs and linked metadata recovery."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import mmo_core as core
import media_tools as media
from support import atomic_json, check_cancel, state_root


@dataclass
class WorkflowResult:
    manifest: Path | None = None
    completed: int = 0
    failures: int = 0
    cancelled: bool = False
    paths: list[Path] = field(default_factory=list)
    nfo_created: int = 0
    tags_updated: int = 0
    dry_run: bool = False


def nfo_bytes(group):
    root = ET.Element('movie')
    for key, value in (('title',group.title),('year',group.year),('plot',group.overview)):
        if value is not None and str(value):
            ET.SubElement(root,key).text=str(value)
    for genre in group.genres:
        ET.SubElement(root,'genre').text=str(genre)
    identifier=getattr(group,'provider_id',None) or group.tmdb_id
    if identifier:
        provider=getattr(group,'provider','TMDB')
        ET.SubElement(root,'uniqueid',{'type':provider.lower(),'default':'true'}).text=str(identifier)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


def _snapshot(path):
    stat=Path(path).stat()
    return {'device':stat.st_dev,'inode':stat.st_ino,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}


def _fd_snapshot(stream):
    import os
    stat=os.fstat(stream.fileno())
    return {'device':stat.st_dev,'inode':stat.st_ino,'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}


def _write_nfo(path,payload,manifest):
    """Journal ownership before writing; never replace an existing sidecar."""
    path=Path(path)
    if path.exists():
        return False
    journal=json.loads(Path(manifest).read_text(encoding='utf-8'))
    record={'path':str(path),'status':'planned','sha256':hashlib.sha256(payload).hexdigest()}
    journal.setdefault('nfo_records',[]).append(record)
    atomic_json(Path(manifest),journal)
    with path.open('xb') as stream:
        record.update(status='writing',snapshot=_fd_snapshot(stream))
        atomic_json(Path(manifest),journal)
        stream.write(payload)
        stream.flush()
        import os
        os.fsync(stream.fileno())
        record['snapshot']=_fd_snapshot(stream)
        atomic_json(Path(manifest),journal)
    if _snapshot(path)!=record['snapshot']:
        raise OSError('Generated NFO was replaced or changed while being written; left in place.')
    record.update(status='done')
    atomic_json(Path(manifest),journal)
    return True


def _tag_jobs(journal_path):
    journal_path=Path(journal_path).resolve()
    paths=[]
    data=json.loads(journal_path.read_text(encoding='utf-8'))
    for candidate in data.get('metadata_edits',[]):
        paths.append(Path(candidate))
    # The link exists in the metadata journal before its first file mutation.
    # Recover even when the application stopped before adding the reverse link.
    for candidate in (state_root()/'metadata').glob('metadata-*.json'):
        try:
            record=json.loads(candidate.read_text(encoding='utf-8'))
            link=record.get('file_manifest')
            if link and Path(link).resolve()==journal_path:
                paths.append(candidate)
        except (OSError,ValueError):
            continue
    return sorted(set(paths),key=lambda p:p.name,reverse=True)


def _rebase_edit(tag_manifest):
    tag_manifest=Path(tag_manifest)
    record=json.loads(tag_manifest.read_text(encoding='utf-8'))
    link=record.get('file_manifest')
    if not link:
        return
    before=record['after'] if record.get('undone') else record['before']
    after=record.get('restored_snapshot') if record.get('undone') else record['after']
    core.rebase_after_metadata(Path(link),Path(record['path']),before,after)
    journal=json.loads(Path(link).read_text(encoding='utf-8'))
    edits=journal.setdefault('metadata_edits',[])
    if str(tag_manifest) not in edits:
        edits.append(str(tag_manifest))
        atomic_json(Path(link),journal)


def restore_tag_edit(manifest,tool_paths=None,cancel_event=None):
    manifest=Path(manifest)
    previous=json.loads(manifest.read_text(encoding='utf-8'))
    if not previous.get('undone') and previous.get('status')=='done' and previous.get('file_manifest'):
        _rebase_edit(manifest)
    result=media.undo_tags(manifest,tool_paths=tool_paths,cancel_event=cancel_event)
    if not previous.get('undone'):
        _rebase_edit(manifest)
    return result


def run(plan,mode='rename',verification='BLAKE3',write_nfo=False,embedded_tags=False,
        replace_comments=False,dry_run=True,journal_dir=None,cancel_event=None,
        log=None,progress=None,tool_paths=None):
    core.validate_plan(plan)
    selected=[g for g in plan if g.approved and g.resolved and g.status!='Conflict']
    if dry_run:
        for group in selected:
            check_cancel(cancel_event)
            if log:
                log(f'DRY RUN {mode.upper()}: {group.source} → {group.destination}')
                if write_nfo:
                    log(f'DRY RUN NFO: {next(c.dst for c in group.files if c.kind=="video").with_suffix(".nfo")}')
                if embedded_tags:
                    log(f'DRY RUN TAGS: {group.title} ({group.year}); comment '+('replaced' if replace_comments else 'preserved'))
        return WorkflowResult(completed=len(selected),dry_run=True)
    # Resolve dependencies/capabilities before any media is transferred.
    if embedded_tags:
        for group in selected:
            for change in group.files:
                if change.kind!='video':
                    continue
                media.validate_tag_update(change.src,year=group.year,
                    comment=f'{group.title} ({group.year})' if replace_comments else None,tool_paths=tool_paths)
    result=core.execute_plan(plan,mode=mode,journal_dir=journal_dir,cancel_event=cancel_event,
                             log=log,progress=progress,verification=verification)
    outcome=WorkflowResult(result.manifest,result.completed,result.failures,result.cancelled,list(result.paths))
    if result.cancelled:
        return outcome
    by_path={str(change.dst.resolve()):(group,change) for group in selected for change in group.files if change.kind=='video'}
    for index,path in enumerate(result.paths,1):
        try:
            check_cancel(cancel_event)
            group,change=by_path[str(Path(path).resolve())]
            if embedded_tags:
                value=f'{group.title} ({group.year})' if replace_comments else None
                edited=media.update_tags(path,group.title,group.year,journal_dir=state_root()/'metadata',
                                         tool_paths=tool_paths,cancel_event=cancel_event,comment=value,
                                         file_manifest=str(result.manifest))
                _rebase_edit(edited['manifest'])
                outcome.tags_updated+=1
                if log:
                    log(f'Embedded title/year'+(' and comment' if replace_comments else '')+f' updated: {path}')
            if write_nfo:
                nfo=Path(path).with_suffix('.nfo')
                if _write_nfo(nfo,nfo_bytes(group),result.manifest):
                    outcome.nfo_created+=1
                    if log: log(f'NFO created: {nfo}')
                elif log:
                    log(f'Existing NFO left unchanged: {nfo}')
        except core.Cancelled:
            outcome.cancelled=True
            break
        except Exception as exc:
            outcome.failures+=1
            journal=json.loads(Path(result.manifest).read_text(encoding='utf-8'))
            journal.setdefault('extra_errors',[]).append({'path':str(path),'error':str(exc)})
            atomic_json(Path(result.manifest),journal)
            if log: log(f'FAILED extra output: {path}: {exc}')
        if progress:
            progress(index,len(result.paths),'Finishing NFO and embedded tags')
    return outcome


def undo_run(manifest,cancel_event=None,log=None,progress=None,tool_paths=None):
    manifest=Path(manifest)
    if json.loads(manifest.read_text(encoding='utf-8')).get('undone'):
        return core.undo_manifest(manifest,cancel_event=cancel_event,log=log,progress=progress)
    for job in _tag_jobs(manifest):
        check_cancel(cancel_event)
        record=json.loads(job.read_text(encoding='utf-8'))
        if record.get('undone'):
            _rebase_edit(job)
            continue
        if record.get('status') in ('prepared','not-applied'):
            continue
        if record.get('status')!='done':
            raise media.MediaToolError('An embedded edit did not complete cleanly. Inspect its recovery record before undoing file operations.')
        restore_tag_edit(job,tool_paths,cancel_event)
        if log: log(f'Original embedded tag values restored: {record["path"]}')
    journal=json.loads(manifest.read_text(encoding='utf-8'))
    for record in reversed(journal.get('nfo_records',[])):
        if record.get('status')=='undone':
            continue
        path=Path(record['path'])
        if not path.exists():
            record['status']='undone'
        else:
            if record.get('status')!='done' or _snapshot(path)!=record['snapshot'] or hashlib.sha256(path.read_bytes()).hexdigest()!=record['sha256']:
                raise OSError(f'Generated NFO changed or is incomplete; left unchanged: {path}')
            path.unlink()
            record['status']='undone'
        atomic_json(manifest,journal)
    return core.undo_manifest(manifest,cancel_event=cancel_event,log=log,progress=progress)
