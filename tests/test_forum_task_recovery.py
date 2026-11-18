import json
import multiprocessing
import sqlite3
from unittest.mock import patch

import pytest
from flask import Flask

import blueprints.forum as forum
from services import forum_tasks as tasks


def leave_task(path, pipe, complete):
    forum.FORUMS_DB = path
    task = tasks.create(path, 7)
    if complete:
        forum._save_forum_to_db(7, {"title":"demo","posts":[{"floor":0,"content":"text","name":"author"}]},
                                "topic","custom","","",None,None, task_id=task)
    pipe.send(task)
    pipe.close()


@pytest.mark.parametrize("complete", [False, True])
def test_poll_after_worker_exit(tmp_path, monkeypatch, complete):
    if 'fork' not in multiprocessing.get_all_start_methods():
        pytest.skip('Linux production worker lifecycle')
    path=str(tmp_path/'forums.db')
    monkeypatch.setattr(forum,'FORUMS_DB',path)
    forum.init_forums_db()
    ctx=multiprocessing.get_context('fork')
    read,write=ctx.Pipe(duplex=False)
    proc=ctx.Process(target=leave_task,args=(path,write,complete))
    proc.start()
    write.close()
    proc.join(8)
    assert proc.exitcode==0
    task=read.recv()
    read.close()
    app=Flask(__name__)
    app.register_blueprint(forum.forum_bp)
    client=app.test_client()
    url='/api/forums/task/'+task
    monkeypatch.setattr(forum,'get_current_user_id',lambda:8)
    assert client.get(url).status_code==404
    monkeypatch.setattr(forum,'get_current_user_id',lambda:7)
    result=client.get(url).json
    if complete:
        assert result['status']=='done' and result['result']['posts'][0]['content']=='text'
        client.delete('/api/forums/'+str(result['result']['id']))
        assert client.get(url).json['status']=='error'
    else:
        assert result['status']=='error' and 'interrupted' in result['error']
        with pytest.raises(ValueError):
            forum._save_forum_to_db(7,{'posts':[]},'late','custom','','',None,None,task_id=task)
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT count(*) FROM forums').fetchone()[0]==0
    with patch.object(tasks.time,'time',return_value=tasks.time.time()+tasks.TTL+1):
        assert client.get(url).status_code==404
