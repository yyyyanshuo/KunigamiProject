"""Destructive operations are tested only against temporary DBs and fake COS."""
import sqlite3
import json
import pytest
from core.account_cleanup import clean_square, clean_image_usage, unlink_sticker_packs
import cos_utils


def test_square_cleanup_keeps_public_content_and_other_accounts(tmp_path):
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE characters(id TEXT, author_email TEXT, author_user_id INTEGER, source_character_id TEXT, base_persona TEXT, comment_count INTEGER)')
        db.executemany('INSERT INTO characters VALUES (?,?,?,?,?,?)', [('a','a@example.test',1,'private','public persona',2),('b','b@example.test',2,'other','other persona',0)])
        db.execute('CREATE TABLE comments(character_id TEXT, author_user_id INTEGER, content TEXT)')
        db.executemany('INSERT INTO comments VALUES (?,?,?)', [('a',1,'mine'),('a',2,'other')])
        for table in ('likes','favorites','square_trial_sessions','square_trial_messages','square_trial_requests'):
            db.execute(f'CREATE TABLE {table}(user_id INTEGER)')
            db.executemany(f'INSERT INTO {table} VALUES (?)', [(1,), (2,)])
        clean_square(db, 1, 'a@example.test')
        assert db.execute('SELECT author_email,author_user_id,source_character_id,base_persona,comment_count FROM characters WHERE id="a"').fetchone() == (None,None,None,'public persona',1)
        assert db.execute('SELECT author_email FROM characters WHERE id="b"').fetchone()[0] == 'b@example.test'
        assert db.execute('SELECT content FROM comments').fetchall() == [('other',)]
        for table in ('likes','favorites','square_trial_sessions','square_trial_messages','square_trial_requests'):
            assert db.execute(f'SELECT user_id FROM {table}').fetchall() == [(2,)]
        clean_square(db, 1, 'a@example.test')  # retry is safe


def test_usage_and_public_stickers(tmp_path):
    path=tmp_path/'usage.db'
    with sqlite3.connect(path) as db:
        for table in ('image_days','image_reservations'):
            db.execute(f'CREATE TABLE {table}(user_id INTEGER)')
            db.executemany(f'INSERT INTO {table} VALUES (?)', [(1,), (2,)])
    clean_image_usage(path, 1)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT user_id FROM image_days').fetchall()==[(2,)]
        assert db.execute('SELECT user_id FROM image_reservations').fetchall()==[(2,)]
    for uid in (1,2):
        pack=tmp_path/'stickers'/str(uid); pack.mkdir(parents=True)
        (pack/'meta.json').write_text(json.dumps({'uploaded_by':uid,'uploaded_by_name':f'user{uid}','name':'Pack'}))
        (pack/'image.png').write_bytes(b'public image')
    unlink_sticker_packs(tmp_path/'stickers', 1)
    meta=json.loads((tmp_path/'stickers/1/meta.json').read_text())
    assert meta=={'name':'Pack','author_deleted':True}
    assert (tmp_path/'stickers/1/image.png').read_bytes()==b'public image'
    assert json.loads((tmp_path/'stickers/2/meta.json').read_text())['uploaded_by']==2


class FakeCOS:
    def __init__(self, versioned=False, fail=False, escaped=False):
        self.versioned=versioned; self.fail=fail; self.escaped=escaped; self.deleted=[]; self.pages=[]
    def get_bucket_versioning(self, **kwargs):
        return {'Status':'Enabled' if self.versioned else ''}
    def list_objects(self, **kwargs):
        assert kwargs['Prefix']=='users/1/'
        self.pages.append(kwargs)
        if not kwargs['Marker']:
            return {'Contents':[{'Key':'users/2/private' if self.escaped else 'users/1/a'}], 'IsTruncated':'true','NextMarker':'users/1/a'}
        return {'Contents':[{'Key':'users/1/b'}], 'IsTruncated':'false'}
    def list_objects_versions(self, **kwargs):
        assert kwargs['Prefix']=='users/1/'
        self.pages.append(kwargs)
        if not kwargs['KeyMarker']:
            return {'Version':[{'Key':'users/1/a','VersionId':'v1'}], 'IsTruncated':'true','NextKeyMarker':'users/1/a','NextVersionIdMarker':'v1'}
        assert kwargs['VersionIdMarker']=='v1'
        return {'DeleteMarker':[{'Key':'users/1/a','VersionId':'v2'}], 'IsTruncated':'false'}
    def delete_object(self, **kwargs):
        if self.fail: raise RuntimeError('simulated deletion failure')
        self.deleted.append(kwargs)


@pytest.mark.parametrize('versioned',[False,True])
def test_cos_scoped_paginated_cleanup(monkeypatch, versioned):
    fake=FakeCOS(versioned)
    monkeypatch.setattr(cos_utils,'LOCAL_TEST',False)
    monkeypatch.setattr(cos_utils,'client',fake)
    monkeypatch.setattr(cos_utils,'bucket','synthetic-bucket')
    cos_utils.delete_user_objects(1)
    assert len(fake.deleted)==2
    assert len(fake.pages)==2
    assert all(x['Key'].startswith('users/1/') for x in fake.deleted)
    if versioned: assert {x['VersionId'] for x in fake.deleted}=={'v1','v2'}


@pytest.mark.parametrize('args',[{'fail':True},{'escaped':True}])
def test_cos_cleanup_fails_closed(monkeypatch,args):
    fake=FakeCOS(**args)
    monkeypatch.setattr(cos_utils,'LOCAL_TEST',False)
    monkeypatch.setattr(cos_utils,'client',fake)
    with pytest.raises(RuntimeError): cos_utils.delete_user_objects(1)
    assert not fake.deleted


def test_cos_offline_and_invalid_user(monkeypatch):
    fake=FakeCOS()
    monkeypatch.setattr(cos_utils,'client',fake)
    monkeypatch.setattr(cos_utils,'LOCAL_TEST',True)
    cos_utils.delete_user_objects(1)
    assert not fake.pages
    with pytest.raises(ValueError): cos_utils.delete_user_objects(-1)


def test_account_checks_phrase_and_cloud_failure_keeps_account(tmp_path, monkeypatch):
    from flask import Flask
    from werkzeug.security import generate_password_hash
    import blueprints.auth as auth
    db_path=tmp_path/'users.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE users(id INTEGER, email TEXT, password_hash TEXT)')
        db.execute('INSERT INTO users VALUES (?,?,?)',(1,'synthetic@example.test',generate_password_hash('test-only')))
    monkeypatch.setattr(auth,'USERS_DB',str(db_path))
    called=[]
    def fail(uid):
        called.append(uid)
        raise RuntimeError('fake cloud denied')
    monkeypatch.setattr(cos_utils,'delete_user_objects',fail)
    app=Flask(__name__); app.secret_key='test-only'; app.register_blueprint(auth.auth_bp)
    client=app.test_client()
    with client.session_transaction() as s: s['user_id']=1
    assert client.post('/api/account/delete',json={'password':'test-only','delete_confirmed':True}).status_code==400
    assert not called
    assert client.post('/api/account/delete',json={'password':'wrong','confirm_text':'DELETE ACCOUNT'}).status_code==403
    assert not called
    result=client.post('/api/account/delete',json={'password':'test-only','confirm_text':'DELETE ACCOUNT'})
    assert result.status_code==503
    assert called==[1]
    with sqlite3.connect(db_path) as db: assert db.execute('SELECT id FROM users').fetchall()==[(1,)]


def test_logs_never_keep_prompt_reply_or_error_body(tmp_path, monkeypatch, capsys):
    from services import ai_client
    monkeypatch.setattr(ai_client,'USERS_ROOT',str(tmp_path))
    monkeypatch.setattr(ai_client,'get_current_user_id',lambda:1)
    log=tmp_path/'1/logs/api.log';log.parent.mkdir(parents=True);log.write_text('OLD_PRIVATE_CONTENT\n')
    ai_client.log_full_prompt('Gemini', [{'role':'user','content':'PRIVATE_PROMPT'}], 'PRIVATE_REPLY')
    ai_client.log_api_error('Gemini',401,'PRIVATE_UPSTREAM_BODY')
    text=log.read_text()+capsys.readouterr().out
    assert all(marker not in text for marker in ('OLD_PRIVATE_CONTENT','PRIVATE_PROMPT','PRIVATE_REPLY','PRIVATE_UPSTREAM_BODY'))
    assert json.loads(log.read_text().splitlines()[-1])['status_code']==401


def test_local_public_avatar_survives_private_cleanup(tmp_path):
    users=tmp_path/'users'; uploads=users/'1/uploads';uploads.mkdir(parents=True)
    (uploads/'avatar.png').write_bytes(b'public avatar')
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE characters(id TEXT,avatar TEXT,author_email TEXT,author_user_id INTEGER,source_character_id TEXT)')
        db.execute('INSERT INTO characters VALUES (?,?,?,?,?)',('a','/local_uploads/avatar.png','a@example.test',1,'private'))
        clean_square(db,1,'a@example.test',users_root=users)
        avatar=db.execute('SELECT avatar FROM characters').fetchone()[0]
        assert avatar.startswith('/public_uploads/')
        assert (tmp_path/avatar.lstrip('/')).read_bytes()==b'public avatar'


def test_offline_dir_fd_deletion_stays_inside_root(tmp_path):
    import os
    from core.runtime import DATA_ROOT
    target=tmp_path/'tree';target.mkdir();(target/'file').write_text('synthetic')
    import shutil
    shutil.rmtree(target)
    assert not target.exists()
    fd=os.open('/tmp',os.O_RDONLY)
    try:
        with pytest.raises(PermissionError): os.unlink('saki-outside-deletion-sentinel',dir_fd=fd)
    finally: os.close(fd)


def test_vision_uses_inline_bytes_and_keeps_local_on_cloud_failure(tmp_path, monkeypatch):
    from flask import Flask
    from PIL import Image
    import io
    import blueprints.media as media
    monkeypatch.setattr(media.core.config,'USERS_ROOT',str(tmp_path/'users'))
    monkeypatch.setattr(media,'get_current_user_id',lambda:1)
    monkeypatch.setattr(media,'upload_to_cos',lambda *args:None)
    sent=[]
    def describe(data):
        sent.append(data)
        return 'synthetic description'
    monkeypatch.setattr(media,'_generate_image_description',describe)
    app=Flask(__name__);app.register_blueprint(media.media_bp)
    payload=io.BytesIO();Image.new('RGB',(5,5),'blue').save(payload,format='PNG');payload.seek(0)
    result=app.test_client().post('/api/vision/upload',data={'file':(payload,'test.png'),'description_mode':'ai'})
    assert result.status_code==200
    assert sent[0].startswith('data:image/jpeg;base64,')
    assert (tmp_path/'users/1/chat_images'/result.json['path']).exists()
    assert not (tmp_path/'static/uploads').exists()


def test_legal_evidence_is_versioned_and_does_not_rewrite_old_consent():
    from core.legal import init_legal_consents_table,record_current_legal_consents
    import hashlib
    with sqlite3.connect(':memory:') as db:
        init_legal_consents_table(db)
        db.execute("INSERT INTO legal_consents(user_id,document_type,document_version,accepted_at) VALUES (1,'terms','1.0','old')")
        record_current_legal_consents(db,1)
        assert db.execute("SELECT document_hash FROM legal_consents WHERE document_type='terms' AND document_version='1.0'").fetchone()[0] is None
        language,digest,snapshot=db.execute("SELECT accepted_language,document_hash,document_snapshot FROM legal_consents WHERE document_type='privacy'").fetchone()
        assert language in ('ja','en','zh-CN')
        assert hashlib.sha256(snapshot.encode()).hexdigest()==digest
        assert 'templates' not in json.loads(snapshot)['document']['path']


def test_custom_endpoint_never_receives_operator_key(monkeypatch):
    from services import model_provider
    from core import model_settings
    import core.utils as utils
    cfg=model_settings.defaults();cfg['active_route']='custom';cfg['routes']['custom']['base_url']='https://untrusted.invalid/v1'
    monkeypatch.setattr(model_provider,'load_config',lambda uid:cfg)
    monkeypatch.setattr(model_provider,'get_user_credential',lambda *args:'')
    monkeypatch.setattr(utils,'OPENROUTER_KEY','operator-secret-must-not-leave')
    with pytest.raises(ValueError,match='Missing API key'):
        model_provider.connection(1)


def test_moments_vision_never_creates_public_copy(tmp_path, monkeypatch):
    from flask import Flask
    from PIL import Image
    import io
    import blueprints.moments as moments
    import blueprints.media as media
    monkeypatch.setattr(moments,'USERS_ROOT',str(tmp_path/'users'))
    monkeypatch.setattr(moments,'get_current_user_id',lambda:1)
    seen=[]
    def fail(data):
        seen.append(data)
        raise RuntimeError('synthetic model failure')
    monkeypatch.setattr(media,'_generate_image_description',fail)
    app=Flask(__name__);app.secret_key='synthetic';app.register_blueprint(moments.moments_bp)
    payload=io.BytesIO();Image.new('RGB',(5,5),'blue').save(payload,format='PNG');payload.seek(0)
    result=app.test_client().post('/api/moments/post',data={'content':'synthetic','images':(payload,'test.png'),'image_metadata':json.dumps([{'mode':'ai','description':''}])})
    assert result.status_code==502
    assert len(seen)==1 and seen[0].startswith('data:image/jpeg;base64,')
    assert not list((tmp_path/'users/1/tmp').glob('*'))


def test_legacy_public_cos_avatar_gets_independent_copy(tmp_path, monkeypatch):
    import blueprints.square as square
    from core.account_cleanup import preserve_square_avatars
    monkeypatch.setattr(square,'COS_BASE_URL','https://synthetic.cos.example')
    source='https://synthetic.cos.example/users/1/avatar.png'
    assert square._private_avatar_cos_keys(1,source)==['users/1/avatar.png']
    assert square._private_avatar_cos_keys(2,source)==[]
    copied=[]
    def copy(char_id,user_id,avatar_url):
        copied.append((char_id,user_id,avatar_url));return 'https://synthetic.cos.example/square/avatars/public.png'
    monkeypatch.setattr(square,'_materialize_square_avatar',copy)
    db_path=tmp_path/'square.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE characters(id TEXT,avatar TEXT,author_email TEXT,author_user_id INTEGER)')
        db.execute('INSERT INTO characters VALUES (?,?,?,?)',('a',source,'synthetic@example.test',1))
    preserve_square_avatars(db_path,1,'synthetic@example.test')
    assert copied==[('a',1,source)]
    with sqlite3.connect(db_path) as db: assert '/square/avatars/' in db.execute('SELECT avatar FROM characters').fetchone()[0]
