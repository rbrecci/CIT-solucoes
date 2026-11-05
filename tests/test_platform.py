import io
import json
import os
from pathlib import Path
import sqlite3
import time
import wave
from datetime import datetime, timedelta, timezone
import pytest
from app import create_app, initialize, seed
from app.services import ServiceError

PASSWORD='TestOnly!2026'
SOLUTION='Reiniciei o roteador remotamente e confirmei o sinal.'

@pytest.fixture(scope='module')
def prepared(tmp_path_factory):
    root=tmp_path_factory.mktemp('cti')
    os.environ['AUDIO_TEMP_DIR']=str(root/'audio')
    os.environ['ANALYSIS_MODE']='demo'
    os.environ['TRANSCRIPTION_MODE']='demo'
    app=create_app({'TESTING':True,'DATABASE':str(root/'test.sqlite')})
    initialize(app);seed(app,PASSWORD)
    return app

@pytest.fixture
def client(prepared): return prepared.test_client()

def login(client,account='cliente'):
    response=client.post('/api/login',json={'email':account+'@demo.cti.local','password':PASSWORD})
    assert response.status_code==200
    with sqlite3.connect(client.application.config['DATABASE']) as db:
        csrf=db.execute('SELECT csrf FROM sessions ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    return {'X-CSRF-Token':csrf}

@pytest.mark.parametrize('role',['cliente','atendente','supervisor','gestor'])
def test_redirect_and_area_boundaries(client,role):
    login(client,role)
    assert client.get('/').location=='/'+role
    assert client.get('/'+role).status_code==200
    for other in ['cliente','atendente','supervisor','gestor']:
        if other!=role: assert client.get('/'+other).status_code==403
    assert client.get('/'+role+'/inventada').status_code==403
    if role=='cliente':
        assert client.get('/cliente/dashboard').status_code==403
        assert 'Dashboard da equipe' not in client.get('/cliente').text

@pytest.mark.parametrize('account,own,foreign',[('cliente',1,3),('cliente',1,6),('atendente',1,3),('atendente',1,6),('atendente',1,5),('supervisor',1,3),('supervisor2',3,1)])
def test_scope_in_queries_and_ids(client,account,own,foreign):
    login(client,account)
    assert client.get(f'/api/tickets/{own}').status_code==200
    assert client.get(f'/api/tickets/{foreign}').status_code==404
    assert client.get(f'/api/feedbacks/{foreign}').status_code==404
    ids={r['id'] for r in client.get('/api/tickets').json}
    assert own in ids and foreign not in ids
    feedback_ids={r['id'] for r in client.get('/api/feedbacks').json}
    assert foreign not in feedback_ids

@pytest.mark.parametrize('account,paths',[('cliente',['metrics','options','reports']),('atendente',['metrics','reports']),('gestor',['tickets','feedbacks','options'])])
def test_api_role_boundaries(client,account,paths):
    login(client,account)
    for path in paths: assert client.get('/api/'+path).status_code==403

def test_no_privilege_escalation(client):
    assert client.post('/api/login',json={'email':'cliente@demo.cti.local','password':PASSWORD,'role':'gestor'}).status_code==400
    headers=login(client)
    assert client.post('/api/users',json={'role':'gestor'},headers=headers).status_code==405
    assert client.post('/api/register',json={'role':'supervisor'},headers=headers).status_code==405
    assert client.post('/api/tickets',json={},headers=headers).status_code==403
    assert client.post('/api/feedbacks/1/reviews',json={},headers=headers).status_code==403

def test_csrf_session_logout(client):
    assert client.get('/api/tickets').status_code==401
    headers=login(client)
    assert client.post('/api/logout').status_code==403
    assert client.post('/api/logout',headers=headers).status_code==200
    assert client.get('/api/tickets').status_code==401

def test_text_confirmation_and_ownership(client):
    headers=login(client)
    data={'ticket_id':1,'text':'Excelente suporte!','origin':'texto','confirmed':True}
    assert client.post('/api/feedbacks',json={**data,'ticket_id':3},headers=headers).status_code==404
    assert client.post('/api/feedbacks',json={**data,'confirmed':False},headers=headers).status_code==400
    assert client.post('/api/feedbacks',json={**data,'role':'gestor'},headers=headers).status_code==400
    response=client.post('/api/feedbacks',json=data,headers=headers)
    assert response.status_code==201 and response.json['protocol'].startswith('FB-')
    saved=client.get('/api/feedbacks/'+str(response.json['id'])).json
    assert saved['text']==data['text']
    assert 'auto_sentiment' not in saved and 'analysis_status' not in saved

def wav_bytes(duration=.1):
    output=io.BytesIO()
    with wave.open(output,'wb') as audio:
        audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000);audio.writeframes(b'\0\0'*int(8000*duration))
    output.seek(0);return output

@pytest.mark.parametrize('origin',['gravado','anexado'])
def test_audio_draft_review_and_cleanup(client,origin):
    headers=login(client)
    response=client.post('/api/transcribe',data={'origin':origin,'audio':(wav_bytes(),'teste.wav','audio/wav')},headers=headers)
    assert response.status_code==200 and response.json['demo']
    assert list(Path(client.application.config['AUDIO_TEMP_DIR']).iterdir())==[]
    draft=response.json['draft_id']
    data={'ticket_id':1,'origin':origin,'text':'Corrigi e confirmei a minha transcrição.','draft_id':draft,'confirmed':False}
    assert client.post('/api/feedbacks',json=data,headers=headers).status_code==400
    response=client.post('/api/feedbacks',json={**data,'confirmed':True},headers=headers)
    assert response.status_code==201
    result=client.get('/api/feedbacks/'+str(response.json['id'])).json
    assert result['text']==data['text'] and result['origin']==origin
    assert not any('audio' in k for k in result)
    assert client.post('/api/feedbacks',json={**data,'confirmed':True},headers=headers).status_code==400

def test_audio_failure_invalid_duration_and_abandoned_cleanup(client,monkeypatch):
    headers=login(client)
    directory=Path(client.application.config['AUDIO_TEMP_DIR'])
    abandoned=directory/'cti-audio-abandoned.wav';abandoned.write_bytes(b'abandoned');os.utime(abandoned,(time.time()-1000,)*2)
    def fail(path):
        assert Path(path).exists()
        raise ServiceError('test error without content')
    monkeypatch.setattr('app.transcribe',fail)
    with sqlite3.connect(client.application.config['DATABASE']) as db:
        count=db.execute('SELECT count(*) FROM feedbacks').fetchone()[0]
    response=client.post('/api/transcribe',data={'origin':'anexado','audio':(wav_bytes(),'voice.wav','audio/wav')},headers=headers)
    assert response.status_code==503 and list(directory.iterdir())==[]
    for audio,name,mime in [(io.BytesIO(b'not audio'),'voice.wav','audio/wav'),(wav_bytes(301),'voice.wav','audio/wav'),(wav_bytes(),'voice.exe','audio/wav'),(wav_bytes(),'voice.wav','text/html')]:
        assert client.post('/api/transcribe',data={'origin':'anexado','audio':(audio,name,mime)},headers=headers).status_code==422
        assert list(directory.iterdir())==[]
    with sqlite3.connect(client.application.config['DATABASE']) as db:
        assert db.execute('SELECT count(*) FROM feedbacks').fetchone()[0]==count

def test_audio_limits_and_draft_ownership(client,prepared):
    headers=login(client)
    assert client.post('/api/transcribe',data={'origin':'anexado','audio':(io.BytesIO(b'x'*(10*1024*1024+1)),'voice.wav','audio/wav')},headers=headers).status_code==413
    assert list(Path(prepared.config['AUDIO_TEMP_DIR']).iterdir())==[]
    result=client.post('/api/transcribe',data={'origin':'anexado','audio':(wav_bytes(),'voice.wav','audio/wav')},headers=headers).json
    data={'ticket_id':3,'origin':'anexado','text':'Texto de outra pessoa.','confirmed':True,'draft_id':result['draft_id']}
    other=prepared.test_client();other_headers=login(other,'cliente2')
    assert other.post('/api/feedbacks',json=data,headers=other_headers).status_code==400

def test_human_review_immutable_analysis_and_team_assignment(client,prepared):
    headers=login(client,'supervisor')
    original=client.get('/api/feedbacks/2').json
    review={'sentiment':'neutro','critical':False,'justification':'Após contato, confirmei a resolução.','responsible_id':2,'status':'resolvido','action':'Cliente contatado e serviço restabelecido.'}
    assert client.post('/api/feedbacks/3/reviews',json=review,headers=headers).status_code==404
    assert client.post('/api/feedbacks/2/reviews',json={**review,'responsible_id':6},headers=headers).status_code==404
    assert client.post('/api/feedbacks/2/reviews',json=review,headers=headers).status_code==201
    result=client.get('/api/feedbacks/2').json
    assert result['auto_sentiment']==original['auto_sentiment']
    assert result['sentiment']=='neutro' and result['critical']==0
    assert result['reviews'][-1]['author_id']==3
    assert result['reviews'][-1]['justification']==review['justification']
    assert result['reviews'][-1]['created']

def test_agent_operations(client):
    headers=login(client,'atendente')
    data={'client_id':1,'subject':'Suporte de teste','channel':'portal'}
    assert client.post('/api/tickets',json={**data,'client_id':5},headers=headers).status_code==404
    assert client.post('/api/tickets',json={**data,'agent_id':6},headers=headers).status_code==400
    response=client.post('/api/tickets',json=data,headers=headers)
    assert response.status_code==201
    assert client.patch('/api/tickets/'+str(response.json['id']),json={'status':'resolvido','solution':SOLUTION},headers=headers).status_code==200
    assert client.patch('/api/tickets/3',json={'status':'resolvido'},headers=headers).status_code==404
    assert client.patch('/api/tickets/5',json={'status':'resolvido'},headers=headers).status_code==404

def test_aggregates_real_counts_and_manager_no_transcripts(client,prepared):
    login(client,'gestor')
    metrics=client.get('/api/metrics').json
    with sqlite3.connect(prepared.config['DATABASE']) as db:
        total=db.execute('SELECT count(*) FROM feedbacks').fetchone()[0]
    assert metrics['received']==total
    assert metrics['pending']+metrics['analyzed']==total
    assert sum(metrics['sentiments'].values())==metrics['analyzed']
    assert len(metrics['teams'])==2
    assert all(secret not in json.dumps(metrics) for secret in ['client_name','client_id','agent_name','summary','evidence','transcript','email'])
    assert client.get('/api/reports').status_code==200
    assert client.get('/api/metrics?start=invalid').status_code==400
    assert client.get('/api/metrics?start=2099-01-01').json['received']==0

def test_analysis_failure_persists_pending(client,monkeypatch):
    headers=login(client)
    def fail(text): raise ServiceError()
    monkeypatch.setattr('app.analyze',fail)
    response=client.post('/api/feedbacks',json={'ticket_id':1,'text':'Falha simulada do provedor.','origin':'texto','confirmed':True},headers=headers)
    assert response.status_code==201
    headers=login(client,'supervisor')
    assert client.get('/api/feedbacks/'+str(response.json['id'])).json['analysis_status']=='pendente'

def test_demo_negative_alone_not_critical():
    from app.services import analyze
    result=analyze('O atendimento foi ruim.')
    assert result['sentiment']=='negativo' and not result['critical'] and result['confidence'] is None

def test_seed_never_overwrites(prepared):
    import click
    with pytest.raises(click.ClickException): seed(prepared,PASSWORD)

@pytest.mark.parametrize('value',[None,True,[],{},'1',-1])
def test_invalid_identifiers_are_rejected(client,value):
    headers=login(client)
    response=client.post('/api/feedbacks',json={'ticket_id':value,'text':'Texto válido.','origin':'texto','confirmed':True},headers=headers)
    assert response.status_code==400

def new_ticket(client,headers,**extra):
    response=client.post('/api/tickets',json={'client_id':1,'subject':'Fluxo de teste','channel':'portal',**extra},headers=headers)
    assert response.status_code==201
    return response.json['id']

def ticket_row(app,tid):
    with sqlite3.connect(app.config['DATABASE']) as connection:
        connection.row_factory=sqlite3.Row
        return dict(connection.execute('SELECT * FROM tickets WHERE id=?',(tid,)).fetchone())

def set_ticket(app,tid,**fields):
    with sqlite3.connect(app.config['DATABASE']) as connection:
        for column,value in fields.items(): connection.execute(f'UPDATE tickets SET {column}=? WHERE id=?',(value,tid))

def ago(**delta): return (datetime.now(timezone.utc)-timedelta(**delta)).isoformat(timespec='seconds')

def test_resolve_needs_solution_and_happens_once(client,prepared):
    headers=login(client,'atendente'); tid=new_ticket(client,headers); url=f'/api/tickets/{tid}'
    assert client.patch(url,json={'status':'resolvido'},headers=headers).status_code==400
    assert client.patch(url,json={'status':'resolvido','solution':'curto'},headers=headers).status_code==400
    for status in ['aberto','em andamento','encerrado']:
        assert client.patch(url,json={'status':status,'solution':SOLUTION},headers=headers).status_code==400
    assert client.patch(url,json={'status':'resolvido','solution':SOLUTION},headers=headers).status_code==200
    row=ticket_row(prepared,tid)
    assert row['status']=='resolvido' and row['solution']==SOLUTION and row['resolved_at']
    assert client.patch(url,json={'status':'resolvido','solution':SOLUTION+' 2'},headers=headers).status_code==409

def test_register_ticket_already_resolved(client,prepared):
    headers=login(client,'atendente')
    row=ticket_row(prepared,new_ticket(client,headers,solution=SOLUTION))
    assert row['status']=='resolvido' and row['solution']==SOLUTION and row['resolved_at']
    response=client.post('/api/tickets',json={'client_id':1,'subject':'Fluxo de teste','channel':'portal','solution':'curto'},headers=headers)
    assert response.status_code==400

def test_client_confirmation_closes_or_reopens(client,prepared):
    agent=login(client,'atendente')
    closes,reopens,unresolved=(new_ticket(client,agent,solution=SOLUTION),new_ticket(client,agent,solution=SOLUTION),new_ticket(client,agent))
    headers=login(client,'cliente')
    assert client.post(f'/api/tickets/{closes}/confirm',json={'outcome':'resolveu'},headers=headers).status_code==200
    row=ticket_row(prepared,closes)
    assert row['status']=='encerrado' and row['closed_at']
    assert client.post(f'/api/tickets/{reopens}/confirm',json={'outcome':'nao_resolveu'},headers=headers).status_code==200
    row=ticket_row(prepared,reopens)
    assert row['status']=='aberto' and row['solution'] is None and row['resolved_at'] is None
    assert client.post(f'/api/tickets/{unresolved}/confirm',json={'outcome':'resolveu'},headers=headers).status_code==409
    assert client.post(f'/api/tickets/{closes}/confirm',json={'outcome':'resolveu'},headers=headers).status_code==409
    assert client.post(f'/api/tickets/{closes}/confirm',json={'outcome':'talvez'},headers=headers).status_code==400
    assert client.post('/api/tickets/3/confirm',json={'outcome':'resolveu'},headers=headers).status_code==404
    agent=login(client,'atendente')
    assert client.post(f'/api/tickets/{reopens}/confirm',json={'outcome':'resolveu'},headers=agent).status_code==403

def test_unanswered_resolution_closes_after_three_days(client,prepared):
    headers=login(client,'atendente')
    old,recent=new_ticket(client,headers,solution=SOLUTION),new_ticket(client,headers,solution=SOLUTION)
    set_ticket(prepared,old,resolved_at=ago(days=4)); set_ticket(prepared,recent,resolved_at=ago(days=2))
    assert prepared.test_cli_runner().invoke(args=['cleanup-audio']).exit_code==0
    row=ticket_row(prepared,old)
    assert row['status']=='encerrado' and row['closed_at']
    assert ticket_row(prepared,recent)['status']=='resolvido'

def test_response_deadline_marks_overdue(client,prepared):
    headers=login(client,'atendente')
    late,fresh,done=new_ticket(client,headers),new_ticket(client,headers),new_ticket(client,headers,solution=SOLUTION)
    set_ticket(prepared,late,created=ago(hours=25)); set_ticket(prepared,done,created=ago(hours=25))
    rows={r['id']:r for r in client.get('/api/tickets').json}
    assert rows[late]['overdue'] is True and rows[fresh]['overdue'] is False and rows[done]['overdue'] is False
    assert datetime.fromisoformat(rows[late]['due_at'])==datetime.fromisoformat(rows[late]['created'])+timedelta(hours=24)

def test_seeded_tickets_keep_status_invariants(prepared):
    with sqlite3.connect(prepared.config['DATABASE']) as connection:
        rows=connection.execute("SELECT status,solution,resolved_at,closed_at FROM tickets WHERE protocol LIKE 'AT-DEMO-%'").fetchall()
    assert rows and all(s in ('aberto','resolvido','encerrado') for s,*_ in rows)
    assert all(sol and res for s,sol,res,_ in rows if s in ('resolvido','encerrado'))
    assert all(closed for s,_,_,closed in rows if s=='encerrado')
