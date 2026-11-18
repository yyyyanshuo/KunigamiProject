import json
import sqlite3
import pytest
from flask import Flask
from core.square_aliases import parse_aliases, search_fold
from tests.test_square_local_update import _setup_square
import blueprints.square as square


def test_alias_limits_and_unicode():
    assert parse_aliases('  Rin \nＲＩＮ\n 糸師 凛 \n\nRin') == ['Rin','糸師 凛']
    assert search_fold('ＲＩＮ') == 'rin'
    with pytest.raises(ValueError): parse_aliases(['x'*81])
    with pytest.raises(ValueError): parse_aliases([str(i) for i in range(11)])


def test_alias_upload_edit_search_ownership_and_legacy(tmp_path,monkeypatch):
    _,db,_=_setup_square(tmp_path,monkeypatch)
    monkeypatch.setattr(square,'get_current_user_id',lambda:1)
    monkeypatch.setattr(square,'_materialize_square_avatar',lambda *args,**kwargs:'/static/default_avatar.png')
    app=Flask(__name__);app.register_blueprint(square.square_bp);c=app.test_client()
    base=dict(id='alias_hero',name='Primary Hero',base_persona='Original persona',relationship_graph='{}',aliases='  Rin\nＲＩＮ\n糸師凛')
    r=c.post('/api/square/upload',data=base);assert r.status_code==200,r.json
    with sqlite3.connect(db) as conn:
        row=conn.execute('SELECT id,name,aliases,base_persona FROM characters WHERE name=?',('Primary Hero',)).fetchone()
    identifier=row[0];assert row[1:] == ('Primary Hero',json.dumps(['Rin','糸師凛'],ensure_ascii=False),'Original persona')
    for query in ['ＲｉＮ','rin','糸師凛','ｐｒｉｍａｒｙ']:
        response=c.get('/api/square/list',query_string={'search':query})
        assert [entry['id'] for entry in response.json]==[identifier],response.json
    assert c.get('/api/square/character/'+identifier).json['character']['aliases']==['Rin','糸師凛']
    form=dict(base,id=identifier,aliases='Alias Two')
    monkeypatch.setattr(square,'get_current_user_id',lambda:2)
    assert c.post('/api/square/update',data=form).status_code==403
    monkeypatch.setattr(square,'get_current_user_id',lambda:1)
    assert c.post('/api/square/update',data=dict(form,aliases='x'*81)).status_code==400
    assert c.post('/api/square/update',data=form).status_code==200
    assert not c.get('/api/square/list',query_string={'search':'rin'}).json
    assert c.get('/api/square/list',query_string={'search':'ＡＬＩＡＳ ＴＷＯ'}).json[0]['id']==identifier
    # Older clients that omit aliases preserve them; empty explicitly clears them.
    form.pop('aliases');assert c.post('/api/square/update',data=form).status_code==200
    assert c.get('/api/square/character/'+identifier).json['character']['aliases']==['Alias Two']
    assert c.post('/api/square/update',data=dict(form,aliases='')).status_code==200
    with sqlite3.connect(db) as conn:
        conn.execute('INSERT INTO characters(id,name) VALUES (?,?)',('legacy','Legacy'))
    assert c.get('/api/square/list',query_string={'search':'legacy'}).json[0]['aliases']==[]
