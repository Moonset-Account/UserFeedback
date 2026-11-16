"""Bounded, fail-closed unified-diff parser for the Scanner Engine."""
from dataclasses import dataclass,field
import re,zlib

MAX_PATH_CHARS=4096
MAX_DIFF_BYTES=4*1024*1024
MAX_LINE_CHARS=256*1024
MAX_FILES=1024
HUNK_RE=re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$')
GIT_RE=re.compile(r'^diff --git a/(\S+) b/(\S+)$')
MODE_RE=re.compile(r'^(old|new) mode ([0-7]{6})$')
FILE_MODE_RE=re.compile(r'^(new file|deleted file) mode ([0-7]{6})$')
BINARY_RE=re.compile(r'^Binary files (.+) and (.+) differ$')
BINARY_BLOCK_RE=re.compile(r'^(literal|delta) (0|[1-9][0-9]*)$')
GIT85='0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{|}~'
GIT85_MAP={char:index for index,char in enumerate(GIT85)}

class DiffParseError(ValueError):pass

@dataclass(frozen=True)
class DiffLine:
    content:str;line_type:str;line_number:int;hunk_id:int

@dataclass
class DiffFile:
    source_path:str;target_path:str
    operation:str='modify';is_binary:bool=False
    old_mode:str|None=None;new_mode:str|None=None
    added_lines:list[DiffLine]=field(default_factory=list)
    removed_lines:list[DiffLine]=field(default_factory=list)
    target_lines:list[DiffLine]=field(default_factory=list)
    rename_from:str|None=None;rename_to:str|None=None
    @property
    def mode_changed(self)->bool:return self.old_mode is not None and self.new_mode is not None and self.old_mode!=self.new_mode
    @property
    def executable_added(self)->bool:
        new_bits=int(self.new_mode,8)&0o111 if self.new_mode is not None else 0
        old_bits=int(self.old_mode,8)&0o111 if self.old_mode is not None else 0
        return bool(new_bits & ~old_bits)
    @property
    def effective_path(self)->str:return self.source_path if self.operation=='delete' else self.target_path

def _path(value:str)->str:
    if not isinstance(value,str) or not value or len(value)>MAX_PATH_CHARS:raise ValueError('invalid path')
    if '\x00' in value or '\\' in value or value.startswith('/') or re.match(r'^[A-Za-z]:',value):raise ValueError('unsafe path')
    if any(ord(char)<32 for char in value):raise ValueError('unsafe path')
    parts=value.split('/')
    if any(part in {'','.', '..'} for part in parts):raise ValueError('unsafe path')
    return '/'.join(parts)


def _safe(raw:str,prefix:bool=True)->str:
    value=raw.strip()
    if value=='/dev/null':return value
    if prefix and (value.startswith('a/') or value.startswith('b/')):value=value[2:]
    try:return _path(value)
    except ValueError as exc:raise DiffParseError(f'unsafe diff path: {raw}') from exc

def _decode_git85(line:str)->bytes:
    if not line:raise DiffParseError('empty Git base85 line')
    first=line[0]
    if 'A'<=first<='Z':decoded_len=ord(first)-ord('A')+1
    elif 'a'<=first<='z':decoded_len=ord(first)-ord('a')+27
    else:raise DiffParseError('invalid Git base85 length prefix')
    encoded=line[1:];expected=((decoded_len+3)//4)*5
    if len(encoded)!=expected:raise DiffParseError('invalid Git base85 line length')
    output=bytearray()
    for offset in range(0,len(encoded),5):
        value=0
        for char in encoded[offset:offset+5]:
            if char not in GIT85_MAP:raise DiffParseError('invalid Git base85 character')
            value=value*85+GIT85_MAP[char]
        if value>0xffffffff:raise DiffParseError('Git base85 group overflow')
        output.extend(value.to_bytes(4,'big'))
    return bytes(output[:decoded_len])

def _varint(data:bytes,pos:int)->tuple[int,int]:
    value=0;shift=0
    while True:
        if pos>=len(data) or shift>63:raise DiffParseError('malformed Git delta varint')
        byte=data[pos];pos+=1;value|=(byte&0x7f)<<shift
        if not byte&0x80:return value,pos
        shift+=7

def _validate_delta(data:bytes,declared:int)->None:
    _,pos=_varint(data,0);target,pos=_varint(data,pos)
    if target!=declared:raise DiffParseError('Git delta target size mismatch')
    produced=0
    while pos<len(data):
        command=data[pos];pos+=1
        if command&0x80:
            for flag in (0x01,0x02,0x04,0x08):
                if command&flag:
                    if pos>=len(data):raise DiffParseError('truncated Git delta copy offset')
                    pos+=1
            size=0
            for shift,flag in ((0,0x10),(8,0x20),(16,0x40)):
                if command&flag:
                    if pos>=len(data):raise DiffParseError('truncated Git delta copy size')
                    size|=data[pos]<<shift;pos+=1
            produced+=size or 0x10000
        elif command:
            if pos+command>len(data):raise DiffParseError('truncated Git delta insert')
            pos+=command;produced+=command
        else:raise DiffParseError('invalid Git delta opcode')
        if produced>declared:raise DiffParseError('Git delta output exceeds declared size')
    if produced!=declared:raise DiffParseError('Git delta output size mismatch')

def _validate_binary_block(kind:str,size:int,chunks:list[bytes])->None:
    if size>MAX_DIFF_BYTES:raise DiffParseError('Git binary output exceeds limit')
    compressed=b''.join(chunks)
    try:
        decoder=zlib.decompressobj();raw=decoder.decompress(compressed,MAX_DIFF_BYTES+1)
        if decoder.unconsumed_tail or len(raw)>MAX_DIFF_BYTES:raise DiffParseError('Git binary output exceeds limit')
        raw+=decoder.flush(MAX_DIFF_BYTES+1-len(raw))
    except zlib.error as exc:raise DiffParseError('invalid Git binary zlib stream') from exc
    if not decoder.eof or decoder.unused_data or len(raw)>MAX_DIFF_BYTES:raise DiffParseError('invalid Git binary zlib stream')
    if kind=='literal':
        if len(raw)!=size:raise DiffParseError('Git literal size mismatch')
    else:_validate_delta(raw,size)

def parse_diff(diff_text:str)->list[DiffFile]:
    if not isinstance(diff_text,str):raise DiffParseError('diff must be text')
    if len(diff_text.encode('utf-8'))>MAX_DIFF_BYTES:raise DiffParseError('diff exceeds 4 MiB limit')
    if not diff_text:return []
    lines=diff_text.replace('\r\n','\n').replace('\r','\n').split('\n')
    if any(len(line)>MAX_LINE_CHARS for line in lines):raise DiffParseError('diff line exceeds 256 KiB limit')
    files=[];current=None;header_seen=False;structural=False
    active=False;remain_s=remain_t=source_no=target_no=hunk_id=0
    hunk_starts=(0,0,0)
    binary_patch=False;binary_kind=None;binary_size=0;binary_chunks=[]

    def rollback():
        nonlocal active,remain_s,remain_t
        if current is not None:
            a,r,t=hunk_starts;del current.added_lines[a:];del current.removed_lines[r:];del current.target_lines[t:]
        active=False;remain_s=remain_t=0

    def finish_hunk():
        if active or remain_s or remain_t:
            rollback();raise DiffParseError('incomplete or malformed hunk')

    def finalize():
        nonlocal current,header_seen
        if current is None:return
        if current.source_path=='/dev/null' and current.target_path=='/dev/null':raise DiffParseError('both diff paths cannot be /dev/null')
        if current.source_path=='/dev/null':current.operation='create'
        elif current.target_path=='/dev/null':current.operation='delete'
        elif current.source_path!=current.target_path:
            if current.rename_from!=current.source_path or current.rename_to!=current.target_path:raise DiffParseError('path change lacks consistent rename metadata')
            current.operation='rename'
        else:current.operation='modify'
        if current.operation in {'modify','rename'} and ((current.old_mode is None)!=(current.new_mode is None)):raise DiffParseError('incomplete mode metadata')
        if current.operation=='create' and current.old_mode is not None:raise DiffParseError('new file has old mode metadata')
        if current.operation=='delete' and current.new_mode is not None:raise DiffParseError('deleted file has new mode metadata')
        if (current.rename_from is None)!=(current.rename_to is None):raise DiffParseError('incomplete rename metadata')
        if current.rename_from is not None and (current.rename_from!=current.source_path or current.rename_to!=current.target_path):raise DiffParseError('rename metadata mismatch')
        if len(files)>=MAX_FILES:raise DiffParseError('diff file-count limit exceeded')
        files.append(current);current=None;header_seen=False

    i=0
    while i<len(lines):
        line=lines[i]
        if binary_patch:
            if line.startswith('diff --git '):
                if binary_kind is None or not binary_chunks:raise DiffParseError('incomplete GIT binary patch')
                _validate_binary_block(binary_kind,binary_size,binary_chunks)
                binary_patch=False;binary_kind=None;binary_size=0;binary_chunks=[]
            elif line=='':i+=1;continue
            else:
                block=BINARY_BLOCK_RE.fullmatch(line)
                if block:
                    if binary_kind is not None:
                        if not binary_chunks:raise DiffParseError('empty GIT binary block')
                        _validate_binary_block(binary_kind,binary_size,binary_chunks)
                    binary_kind=block.group(1);binary_size=int(block.group(2));binary_chunks=[];i+=1;continue
                if binary_kind is None:raise DiffParseError('payload before Git binary block')
                binary_chunks.append(_decode_git85(line));i+=1;continue
        if active:
            if line.startswith('+') and remain_t>0:
                item=DiffLine(line[1:],'added',target_no,hunk_id);current.added_lines.append(item);current.target_lines.append(item);target_no+=1;remain_t-=1
            elif line.startswith('-') and remain_s>0:
                current.removed_lines.append(DiffLine(line[1:],'removed',source_no,hunk_id));source_no+=1;remain_s-=1
            elif line.startswith(' ') and remain_s>0 and remain_t>0:
                item=DiffLine(line[1:],'context',target_no,hunk_id);current.target_lines.append(item);source_no+=1;target_no+=1;remain_s-=1;remain_t-=1
            elif line=='\\ No newline at end of file':pass
            else:rollback();raise DiffParseError('malformed hunk payload')
            active=bool(remain_s or remain_t);i+=1;continue
        if line=='':i+=1;continue
        if line=='\\ No newline at end of file':
            if current is None:raise DiffParseError('newline marker without file')
            i+=1;continue
        git=GIT_RE.fullmatch(line)
        if line.startswith('diff --git '):
            if not git:raise DiffParseError('unsupported or ambiguous git path header')
            finish_hunk();finalize();current=DiffFile(_safe('a/'+git.group(1)),_safe('b/'+git.group(2)));header_seen=False;structural=True;i+=1;continue
        if line.startswith('--- ') and i+1<len(lines) and lines[i+1].startswith('+++ '):
            source=_safe(line[4:]);target=_safe(lines[i+1][4:])
            if current is not None and not header_seen:
                if source!='/dev/null' and current.source_path!=source:raise DiffParseError('git and file headers disagree')
                if target!='/dev/null' and current.target_path!=target:raise DiffParseError('git and file headers disagree')
                current.source_path=source;current.target_path=target
            else:
                finalize();current=DiffFile(source,target)
            header_seen=True;structural=True;i+=2;continue
        if line.startswith('--- ') or line.startswith('+++ '):raise DiffParseError('incomplete file header')
        match=HUNK_RE.fullmatch(line)
        if line.startswith('@@'):
            if not match or current is None:raise DiffParseError('malformed or headerless hunk')
            count_s=int(match.group(2) or '1');count_t=int(match.group(4) or '1');start_s=int(match.group(1));start_t=int(match.group(3))
            if (start_s==0 and count_s!=0) or (start_t==0 and count_t!=0):raise DiffParseError('zero hunk start requires zero count')
            remain_s=count_s;remain_t=count_t;source_no=start_s;target_no=start_t;hunk_id+=1
            hunk_starts=(len(current.added_lines),len(current.removed_lines),len(current.target_lines));active=bool(remain_s or remain_t);structural=True;i+=1;continue
        file_mode=FILE_MODE_RE.fullmatch(line)
        if file_mode:
            if current is None:raise DiffParseError('file mode metadata without file')
            if file_mode.group(1)=='new file':
                if current.new_mode is not None:raise DiffParseError('duplicate new mode metadata')
                current.new_mode=file_mode.group(2);current.source_path='/dev/null'
            else:
                if current.old_mode is not None:raise DiffParseError('duplicate old mode metadata')
                current.old_mode=file_mode.group(2);current.target_path='/dev/null'
            structural=True;i+=1;continue
        mode=MODE_RE.fullmatch(line)
        if mode:
            if current is None:raise DiffParseError('mode metadata without file')
            if mode.group(1)=='old':
                if current.old_mode is not None:raise DiffParseError('duplicate old mode metadata')
                current.old_mode=mode.group(2)
            else:
                if current.new_mode is not None:raise DiffParseError('duplicate new mode metadata')
                current.new_mode=mode.group(2)
            structural=True;i+=1;continue
        if line.startswith('rename from '):
            if current is None:raise DiffParseError('rename metadata without file')
            current.rename_from=_safe(line[12:],False);structural=True;i+=1;continue
        if line.startswith('rename to '):
            if current is None:raise DiffParseError('rename metadata without file')
            current.rename_to=_safe(line[10:],False);structural=True;i+=1;continue
        if line.startswith('similarity index ') or line.startswith('index '):i+=1;continue
        binary=BINARY_RE.fullmatch(line)
        if line.startswith('Binary files '):
            if current is None or not binary:raise DiffParseError('malformed binary marker')
            source=_safe(binary.group(1));target=_safe(binary.group(2))
            if current.source_path!=source:
                if source=='/dev/null' and current.source_path!='/dev/null':current.source_path=source
                else:raise DiffParseError('git and binary paths disagree')
            if current.target_path!=target:
                if target=='/dev/null' and current.target_path!='/dev/null':current.target_path=target
                else:raise DiffParseError('git and binary paths disagree')
            current.is_binary=True;structural=True;i+=1;continue
        if line=='GIT binary patch':
            if current is None:raise DiffParseError('binary marker without file')
            current.is_binary=True;binary_patch=True;binary_kind=None;binary_size=0;binary_chunks=[];structural=True;i+=1;continue
        raise DiffParseError(f'unrecognized diff structure: {line[:40]}')
    if binary_patch:
        if binary_kind is None or not binary_chunks:raise DiffParseError('incomplete GIT binary patch')
        _validate_binary_block(binary_kind,binary_size,binary_chunks)
    finish_hunk();finalize()
    if not structural:raise DiffParseError('non-empty input is not a unified diff')
    return files
