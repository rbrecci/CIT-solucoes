import csv
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
import click
from flask import Flask, g, request, jsonify, render_template, redirect, make_response, abort
from werkzeug.security import generate_password_hash, check_password_hash
from .services import analyze, transcribe, ServiceError

ROLES = {
 'cliente': [('atendimentos','Meus atendimentos'),('enviar','Enviar feedback'),('feedbacks','Meus feedbacks')],
 'atendente': [('atendimentos','Meus atendimentos'),('registrar','Registrar atendimento'),('feedbacks','Feedbacks dos meus atendimentos')],
 'supervisor': [('dashboard','Dashboard da equipe'),('atendimentos','Atendimentos da equipe'),('feedbacks','Feedbacks da equipe'),('criticos','Casos críticos')],
 'gestor': [('dashboard','Visão geral'),('tendencias','Indicadores e tendências'),('equipes','Comparação entre equipes'),('relatorios','Relatórios')]
}

RESPONSE_HOURS=24
CONFIRMATION_DAYS=3
SOLUTION_MIN,SOLUTION_MAX=10,2000

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def db():
    if 'db' not in g:
        g.db=sqlite3.connect(g.app_db, timeout=15)
        g.db.row_factory=sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db

def create_app(test_config=None):
    app=Flask(__name__,instance_relative_config=True)
    app.config.update(DATABASE=os.environ.get('DATABASE_PATH',str(Path(app.instance_path)/'cti.sqlite')),MAX_CONTENT_LENGTH=12*1024*1024,COOKIE_SECURE=os.environ.get('COOKIE_SECURE','false').lower()=='true')
    if test_config:
        app.config.update(test_config)
    Path(app.config['DATABASE']).parent.mkdir(parents=True,exist_ok=True)
    tmp=Path(os.environ.get('AUDIO_TEMP_DIR',str(Path(app.instance_path)/'audio-tmp')))
    tmp.mkdir(parents=True,exist_ok=True,mode=0o700)
    app.config['AUDIO_TEMP_DIR']=str(tmp)

    @app.before_request
    def context():
        g.app_db=app.config['DATABASE']
        g.user=None
        g.session=None
        if Path(g.app_db).exists():
            try:
                token=request.cookies.get('cti_session','')
                row=db().execute('SELECT s.*,u.name,u.email,u.role,u.team_id FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),int(time.time()))).fetchone()
                if row:
                    g.session=dict(row)
                    g.user=dict(id=row['user_id'],name=row['name'],email=row['email'],role=row['role'],team_id=row['team_id'])
            except sqlite3.OperationalError:
                pass
        if request.method in ('POST','PUT','PATCH','DELETE') and request.path!='/api/login':
            if not g.user:
                abort(401)
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token',''),g.session['csrf']):
                abort(403)
        if request.path=='/api/login' and request.headers.get('Origin') and request.headers['Origin']!=request.host_url.rstrip('/'):
            abort(403)

    @app.teardown_appcontext
    def close(_):
        connection=g.pop('db',None)
        if connection:
            connection.close()

    @app.after_request
    def security(response):
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        return response

    for code in (400,401,403,404,405,409,413,422,429,500,503):
        def handler(error):
            messages={400:'Dados inválidos.',401:'Entre para continuar.',403:'Acesso não autorizado.',404:'Registro ou página não encontrado.',405:'Operação indisponível.',409:'Operação não permitida para este registro.',413:'Arquivo acima do limite de 10 MB.',422:'Arquivo inválido. Use áudio MP3, WAV, OGG, M4A ou WebM, com até 10 MB e 5 minutos.',429:'Muitas tentativas. Aguarde 15 minutos.',500:'Não foi possível concluir. Tente novamente.',503:'Serviço indisponível. Tente novamente ou escreva seu feedback.'}
            if request.path.startswith('/api/'):
                return jsonify(error=messages.get(error.code,'Falha na operação.')),error.code
            return render_template('error.html',message=messages.get(error.code,'Erro'),code=error.code),error.code
        app.register_error_handler(code,handler)

    def require(*roles):
        def deco(fn):
            @wraps(fn)
            def wrapped(*a,**kw):
                if not g.user:
                    abort(401)
                if g.user['role'] not in roles:
                    abort(403)
                return fn(*a,**kw)
            return wrapped
        return deco

    def payload(allowed,required=()):
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data)-set(allowed) or any(k not in data for k in required):
            abort(400)
        return data

    def text_value(data,key,minimum=1,maximum=10000):
        value=data.get(key,'')
        if not isinstance(value,str) or not minimum<=len(value.strip())<=maximum:
            abort(400)
        return value.strip()

    def scope(alias='t'):
        user=g.user
        if user['role']=='cliente': return f'{alias}.client_id=?',[user['id']]
        if user['role']=='atendente': return f'{alias}.agent_id=?',[user['id']]
        if user['role']=='supervisor': return f'{alias}.team_id=?',[user['team_id']]
        abort(403)

    def ticket(ticket_id):
        cond,args=scope()
        row=db().execute(f'SELECT t.* FROM tickets t WHERE t.id=? AND {cond}',[ticket_id]+args).fetchone()
        if not row: abort(404)
        return dict(row)

    def with_deadline(row):
        due=datetime.fromisoformat(row['created'])+timedelta(hours=RESPONSE_HOURS)
        row['due_at']=due.isoformat(timespec='seconds')
        row['overdue']=row['status'] in ('aberto','em andamento') and datetime.now(timezone.utc)>due
        return row

    def feedback_rows():
        cond,args=scope()
        rows=db().execute(f'''SELECT f.*,t.subject,t.channel,t.agent_id,t.team_id,c.name client_name,u.name agent_name,a.sentiment auto_sentiment,a.confidence,a.summary,a.topics,a.critical auto_critical,a.evidence,a.provider,r.sentiment reviewed_sentiment,r.critical reviewed_critical,r.status case_status,r.action,r.responsible_id,r.justification,r.created reviewed_at,r.author_id
        FROM feedbacks f JOIN tickets t ON t.id=f.ticket_id JOIN users c ON c.id=f.client_id JOIN users u ON u.id=t.agent_id LEFT JOIN analyses a ON a.feedback_id=f.id LEFT JOIN reviews r ON r.id=(SELECT max(id) FROM reviews WHERE feedback_id=f.id) WHERE {cond} ORDER BY f.created DESC''',args).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            if g.user['role']=='cliente':
                item={k:item[k] for k in ['id','protocol','ticket_id','text','origin','created','subject']}
            else:
                for k in ['topics','evidence']:
                    item[k]=json.loads(item[k] or '[]')
                item['sentiment']=item['reviewed_sentiment'] or item['auto_sentiment']
                item['critical']=item['reviewed_critical'] if item['reviewed_critical'] is not None else item['auto_critical']
            result.append(item)
        return result

    def filtered(rows):
        for key in ['client_id','agent_id','channel','origin','sentiment']:
            value=request.args.get(key)
            if value: rows=[r for r in rows if str(r.get(key))==value]
        for key in ['start','end']:
            value=request.args.get(key)
            if value:
                try: datetime.strptime(value,'%Y-%m-%d')
                except ValueError: abort(400)
                rows=[r for r in rows if (r['created'][:10]>=value if key=='start' else r['created'][:10]<=value)]
        topic=request.args.get('topic','').strip().lower()
        if topic: rows=[r for r in rows if any(topic in t.lower() for t in r.get('topics',[]))]
        critical=request.args.get('critical')
        if critical in ('0','1'): rows=[r for r in rows if bool(r.get('critical'))==(critical=='1')]
        return rows

    @app.get('/')
    def index():
        return redirect('/'+g.user['role'] if g.user else '/login')

    @app.get('/login')
    def login_page():
        if g.user: return redirect('/'+g.user['role'])
        return render_template('login.html')

    @app.post('/api/login')
    def login():
        data=payload(['email','password'],['email','password'])
        email=text_value(data,'email',3,254).lower()
        password=text_value(data,'password',1,256)
        key=hashlib.sha256((request.remote_addr+':'+email).encode()).hexdigest()
        attempt=db().execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
        if attempt and attempt['failures']>=8 and attempt['until']>time.time(): abort(429)
        user=db().execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        valid=check_password_hash(user['password'] if user else generate_password_hash('invalid-account-password'),password)
        if not user or not valid:
            failures=attempt['failures']+1 if attempt and attempt['until']>time.time() else 1
            db().execute('INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)',(key,failures,int(time.time())+900))
            db().commit()
            return jsonify(error='E-mail ou senha incorretos.'),401
        db().execute('DELETE FROM login_attempts WHERE key=?',(key,))
        raw=secrets.token_urlsafe(32)
        csrf=secrets.token_urlsafe(32)
        db().execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user['id'],csrf,int(time.time())+28800))
        db().commit()
        response=jsonify(redirect='/'+user['role'])
        response.set_cookie('cti_session',raw,httponly=True,secure=app.config['COOKIE_SECURE'],samesite='Lax',max_age=28800)
        return response

    @app.post('/api/logout')
    @require(*ROLES)
    def logout():
        db().execute('DELETE FROM sessions WHERE token=?',(g.session['token'],)); db().commit()
        response=jsonify(ok=True); response.delete_cookie('cti_session'); return response

    @app.get('/<role>')
    @app.get('/<role>/<tab>')
    def area(role,tab=None):
        if role not in ROLES: abort(404)
        if not g.user: return redirect('/login')
        if g.user['role']!=role: abort(403)
        tab=tab or ROLES[role][0][0]
        if tab not in dict(ROLES[role]): abort(403)
        return render_template('area.html',user=g.user,tabs=ROLES[role],tab=tab,csrf=g.session['csrf'],title=dict(ROLES[role])[tab],demo=os.environ.get('ANALYSIS_MODE','demo')=='demo',transcription_demo=os.environ.get('TRANSCRIPTION_MODE','demo')=='demo')

    @app.get('/api/tickets')
    @require('cliente','atendente','supervisor')
    def tickets():
        cond,args=scope()
        rows=db().execute(f'SELECT t.*,c.name client_name,u.name agent_name FROM tickets t JOIN users c ON c.id=t.client_id JOIN users u ON u.id=t.agent_id WHERE {cond} ORDER BY t.created DESC',args).fetchall()
        return jsonify([with_deadline(dict(r)) for r in rows])

    @app.get('/api/tickets/<int:ticket_id>')
    @require('cliente','atendente','supervisor')
    def get_ticket(ticket_id): return jsonify(with_deadline(ticket(ticket_id)))

    @app.get('/api/options')
    @require('atendente','supervisor')
    def options():
        if g.user['role']=='atendente':
            clients=db().execute("SELECT id,name FROM users WHERE role='cliente' AND team_id=?",(g.user['team_id'],)).fetchall()
            agents=[]
        else:
            clients=db().execute("SELECT id,name FROM users WHERE role='cliente' AND team_id=?",(g.user['team_id'],)).fetchall()
            agents=db().execute("SELECT id,name FROM users WHERE role='atendente' AND team_id=?",(g.user['team_id'],)).fetchall()
        return jsonify(clients=[dict(r) for r in clients],agents=[dict(r) for r in agents])

    @app.post('/api/tickets')
    @require('atendente')
    def new_ticket():
        data=payload(['client_id','subject','channel','solution'],['client_id','subject','channel'])
        if type(data['client_id']) is not int or data['client_id']<=0: abort(400)
        client=db().execute("SELECT id FROM users WHERE id=? AND role='cliente' AND team_id=?",(data['client_id'],g.user['team_id'])).fetchone()
        if not client: abort(404)
        subject=text_value(data,'subject',3,180)
        if data['channel'] not in ['chat','telefone','email','portal']: abort(400)
        solution=text_value(data,'solution',SOLUTION_MIN,SOLUTION_MAX) if 'solution' in data else None
        protocol='AT-'+secrets.token_hex(5).upper()
        created=now()
        cursor=db().execute('INSERT INTO tickets(protocol,client_id,agent_id,team_id,subject,channel,status,created,solution,resolved_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(protocol,client['id'],g.user['id'],g.user['team_id'],subject,data['channel'],'resolvido' if solution else 'aberto',created,solution,created if solution else None))
        db().commit()
        return jsonify(id=cursor.lastrowid,protocol=protocol),201

    @app.patch('/api/tickets/<int:ticket_id>')
    @require('atendente')
    def edit_ticket(ticket_id):
        ticket(ticket_id)
        data=payload(['status','solution'],['status','solution'])
        if data['status']!='resolvido': abort(400)
        solution=text_value(data,'solution',SOLUTION_MIN,SOLUTION_MAX)
        changed=db().execute("UPDATE tickets SET status='resolvido',solution=?,resolved_at=? WHERE id=? AND status IN ('aberto','em andamento')",(solution,now(),ticket_id)).rowcount
        db().commit()
        if not changed: abort(409)
        return jsonify(ok=True)

    @app.post('/api/tickets/<int:ticket_id>/confirm')
    @require('cliente')
    def confirm_ticket(ticket_id):
        ticket(ticket_id)
        data=payload(['outcome'],['outcome'])
        if data['outcome'] not in ['resolveu','nao_resolveu']: abort(400)
        if data['outcome']=='resolveu':
            changed=db().execute("UPDATE tickets SET status='encerrado',closed_at=? WHERE id=? AND status='resolvido'",(now(),ticket_id)).rowcount
        else:
            changed=db().execute("UPDATE tickets SET status='aberto',solution=NULL,resolved_at=NULL WHERE id=? AND status='resolvido'",(ticket_id,)).rowcount
        db().commit()
        if not changed: abort(409)
        return jsonify(ok=True)

    @app.get('/api/feedbacks')
    @require('cliente','atendente','supervisor')
    def feedbacks(): return jsonify(filtered(feedback_rows()))

    @app.get('/api/feedbacks/<int:fid>')
    @require('cliente','atendente','supervisor')
    def get_feedback(fid):
        row=next((r for r in feedback_rows() if r['id']==fid),None)
        if not row: abort(404)
        if g.user['role']=='supervisor':
            row['reviews']=[dict(r) for r in db().execute('SELECT r.*,u.name author_name FROM reviews r JOIN users u ON u.id=r.author_id WHERE feedback_id=? ORDER BY r.id',(fid,))]
        return jsonify(row)

    def run_analysis(fid,text):
        try:
            result=analyze(text)
            db().execute('INSERT OR IGNORE INTO analyses(feedback_id,sentiment,confidence,summary,topics,critical,evidence,provider,created) VALUES(?,?,?,?,?,?,?,?,?)',(fid,result['sentiment'],result['confidence'],result['summary'],json.dumps(result['topics']),int(result['critical']),json.dumps(result['evidence']),result['provider'],now()))
            db().execute("UPDATE feedbacks SET analysis_status='analisado' WHERE id=?",(fid,))
        except ServiceError:
            db().execute("UPDATE feedbacks SET analysis_status='pendente' WHERE id=?",(fid,))
        db().commit()

    @app.post('/api/feedbacks')
    @require('cliente')
    def new_feedback():
        data=payload(['ticket_id','text','origin','draft_id','confirmed'],['ticket_id','text','origin','confirmed'])
        if data['confirmed'] is not True: abort(400)
        if type(data['ticket_id']) is not int or data['ticket_id']<=0: abort(400)
        ticket(data['ticket_id'])
        text=text_value(data,'text',3,10000)
        origin=data['origin']
        if origin not in ['texto','gravado','anexado']: abort(400)
        if origin!='texto':
            if not isinstance(data.get('draft_id'),str): abort(400)
            draft=db().execute('SELECT * FROM drafts WHERE id=? AND user_id=? AND expires>?',(data.get('draft_id'),g.user['id'],int(time.time()))).fetchone()
            if not draft or draft['origin']!=origin: abort(400)
            if text.startswith('[DEMONSTRAÇÃO'): return jsonify(error='Substitua o exemplo pelo seu texto antes de confirmar.'),400
            db().execute('DELETE FROM drafts WHERE id=?',(draft['id'],))
        protocol='FB-'+secrets.token_hex(5).upper()
        cursor=db().execute('INSERT INTO feedbacks(protocol,ticket_id,client_id,text,origin,created) VALUES(?,?,?,?,?,?)',(protocol,data['ticket_id'],g.user['id'],text,origin,now()))
        db().commit()
        run_analysis(cursor.lastrowid,text)
        return jsonify(id=cursor.lastrowid,protocol=protocol),201

    @app.post('/api/feedbacks/<int:fid>/analyze')
    @require('supervisor')
    def retry_analysis(fid):
        row=next((r for r in feedback_rows() if r['id']==fid),None)
        if not row: abort(404)
        if row['analysis_status']=='analisado': abort(409)
        run_analysis(fid,row['text'])
        return jsonify(ok=True)

    @app.post('/api/feedbacks/<int:fid>/reviews')
    @require('supervisor')
    def review(fid):
        if not any(r['id']==fid for r in feedback_rows()): abort(404)
        data=payload(['sentiment','critical','justification','responsible_id','status','action'],['sentiment','critical','justification','status','action'])
        if data['sentiment'] not in ['positivo','neutro','negativo'] or not isinstance(data['critical'],bool) or data['status'] not in ['aberto','em andamento','resolvido']: abort(400)
        reason=text_value(data,'justification',5,2000)
        action=text_value(data,'action',3,2000)
        responsible=data.get('responsible_id') or None
        if responsible is not None and (type(responsible) is not int or responsible<=0): abort(400)
        if responsible and not db().execute("SELECT id FROM users WHERE id=? AND role='atendente' AND team_id=?",(responsible,g.user['team_id'])).fetchone(): abort(404)
        db().execute('INSERT INTO reviews(feedback_id,author_id,sentiment,critical,justification,responsible_id,status,action,created) VALUES(?,?,?,?,?,?,?,?,?)',(fid,g.user['id'],data['sentiment'],int(data['critical']),reason,responsible,data['status'],action,now()))
        db().commit(); return jsonify(ok=True),201

    def cleanup():
        for path in tmp.glob('cti-audio-*'):
            if path.is_file() and path.stat().st_mtime<time.time()-900:
                path.unlink(missing_ok=True)
        db().execute('DELETE FROM drafts WHERE expires<?',(int(time.time()),))
        db().execute('DELETE FROM sessions WHERE expires<?',(int(time.time()),))
        limit=(datetime.now(timezone.utc)-timedelta(days=CONFIRMATION_DAYS)).isoformat(timespec='seconds')
        db().execute("UPDATE tickets SET status='encerrado',closed_at=? WHERE status='resolvido' AND resolved_at<=?",(now(),limit))
        db().commit()

    @app.post('/api/transcribe')
    @require('cliente')
    def audio():
        cleanup()
        if set(request.form)-{'origin'} or set(request.files)!={'audio'}: abort(400)
        origin=request.form.get('origin')
        if origin not in ['gravado','anexado']: abort(400)
        upload=request.files['audio']
        extension=Path(upload.filename or '').suffix.lower()
        if extension not in ['.mp3','.wav','.webm','.ogg','.m4a']: abort(422)
        if upload.mimetype not in ['audio/mpeg','audio/mp3','audio/wav','audio/x-wav','audio/webm','audio/ogg','audio/mp4','video/webm','application/octet-stream']: abort(422)
        fd,path=tempfile.mkstemp(prefix='cti-audio-',suffix=extension,dir=tmp)
        try:
            with os.fdopen(fd,'wb') as output:
                total=0
                while chunk:=upload.stream.read(65536):
                    total+=len(chunk)
                    if total>10*1024*1024: abort(413)
                    output.write(chunk)
            try:
                probe=subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',path],capture_output=True,timeout=15,check=True)
                info=json.loads(probe.stdout)
                audio_streams=[s for s in info['streams'] if s['codec_type']=='audio']
                if not audio_streams or any(s['codec_type']=='video' for s in info['streams']): abort(422)
                duration=float(info.get('format',{}).get('duration',audio_streams[0].get('duration',0)))
                # Browser WebM may omit duration: decode to null, bounding processing time.
                if duration<=0:
                    decoded=subprocess.run(['ffmpeg','-v','error','-i',path,'-t','301','-f','null','-','-progress','pipe:1'],capture_output=True,timeout=20,check=True)
                    stamps=[int(line.split('=')[1]) for line in decoded.stdout.decode().splitlines() if line.startswith('out_time_us=') and line.split('=')[1].isdigit()]
                    duration=max(stamps,default=0)/1_000_000
                if not 0<duration<=300: abort(422)
            except (subprocess.SubprocessError,ValueError,KeyError,FileNotFoundError): abort(422)
            try: result=transcribe(path)
            except ServiceError: abort(503)
            draft=secrets.token_urlsafe(24)
            db().execute('INSERT INTO drafts VALUES(?,?,?,?,?)',(draft,g.user['id'],result,origin,int(time.time())+1800)); db().commit()
            return jsonify(text=result,draft_id=draft,demo=os.environ.get('TRANSCRIPTION_MODE','demo')=='demo')
        finally:
            Path(path).unlink(missing_ok=True)

    def metrics():
        if g.user['role']=='supervisor':
            rows=filtered(feedback_rows())
        else:
            # Managers only receive aggregates: no client text, names, IDs or transcripts.
            rows=[]
            for row in db().execute('''SELECT f.created,f.analysis_status,t.team_id,t.channel,f.origin,a.sentiment,a.critical,a.topics,r.sentiment reviewed_sentiment,r.critical reviewed_critical,r.status case_status FROM feedbacks f JOIN tickets t ON t.id=f.ticket_id LEFT JOIN analyses a ON a.feedback_id=f.id LEFT JOIN reviews r ON r.id=(SELECT max(id) FROM reviews WHERE feedback_id=f.id)'''):
                item=dict(row); item['sentiment']=item['reviewed_sentiment'] or item['sentiment']; item['critical']=item['reviewed_critical'] if item['reviewed_critical'] is not None else item['critical']; item['topics']=json.loads(item['topics'] or '[]'); rows.append(item)
            rows=filtered(rows)
        analyzed=[r for r in rows if r['analysis_status']=='analisado']
        sentiments={k:sum(r['sentiment']==k for r in analyzed) for k in ['positivo','neutro','negativo']}
        topics={}
        days={}
        critical={k:0 for k in ['aberto','em andamento','resolvido']}
        for r in rows:
            day=r['created'][:10]
            days.setdefault(day,dict(positivo=0,neutro=0,negativo=0))
            if r.get('sentiment'): days[day][r['sentiment']]+=1
            if r.get('critical'): critical[r.get('case_status') or 'aberto']+=1
            if r['analysis_status']=='analisado':
                for topic in r['topics']: topics[topic]=topics.get(topic,0)+1
        teams=[]
        for team in db().execute('SELECT * FROM teams'):
            if g.user['role']=='supervisor' and team['id']!=g.user['team_id']: continue
            subset=[r for r in rows if r['team_id']==team['id']]
            teams.append(dict(name=team['name'],received=len(subset),analyzed=sum(r['analysis_status']=='analisado' for r in subset),positive=sum(r.get('sentiment')=='positivo' for r in subset),critical=sum(bool(r.get('critical')) for r in subset),resolved=sum(bool(r.get('critical')) and r.get('case_status')=='resolvido' for r in subset)))
        return dict(received=len(rows),analyzed=len(analyzed),pending=len(rows)-len(analyzed),sentiments=sentiments,critical=critical,topics=sorted(topics.items(),key=lambda v:-v[1]),timeline=[dict(date=k,**v) for k,v in sorted(days.items())],teams=teams)

    @app.get('/api/metrics')
    @require('supervisor','gestor')
    def dashboard(): return jsonify(metrics())

    @app.get('/api/reports')
    @require('gestor')
    def reports():
        output=io.StringIO(); writer=csv.writer(output); writer.writerow(['Equipe','Recebidos','Analisados','Positivos','Críticos','Resolvidos'])
        for team in metrics()['teams']: writer.writerow([team[k] for k in ['name','received','analyzed','positive','critical','resolved']])
        response=make_response('\ufeff'+output.getvalue()); response.headers['Content-Type']='text/csv; charset=utf-8'; response.headers['Content-Disposition']='attachment; filename=cti-indicadores.csv'; return response

    @app.cli.command('init-db')
    def init_command():
        initialize(app)
        click.echo('Migração aplicada.')

    @app.cli.command('seed-demo')
    @click.option('--password',envvar='DEMO_PASSWORD',required=True,hide_input=True)
    def seed_command(password):
        if len(password)<12: raise click.ClickException('Use senha de pelo menos 12 caracteres.')
        seed(app,password); click.echo('Dados fictícios criados. Contas @demo.cti.local; senha fornecida pelo operador.')

    @app.cli.command('create-user')
    @click.option('--name',prompt=True)
    @click.option('--email',prompt=True)
    @click.option('--role',type=click.Choice(list(ROLES)),prompt=True)
    @click.option('--team',type=int)
    @click.password_option()
    def create_user(name,email,role,team,password):
        if len(password)<12: raise click.ClickException('Senha mínima: 12 caracteres.')
        g.app_db=app.config['DATABASE']
        if role!='gestor' and not db().execute('SELECT id FROM teams WHERE id=?',(team,)).fetchone(): raise click.ClickException('Informe uma equipe válida com --team.')
        db().execute('INSERT INTO users(name,email,password,role,team_id) VALUES(?,?,?,?,?)',(name,email.lower(),generate_password_hash(password),role,team)); db().commit(); click.echo('Conta criada.')

    @app.cli.command('cleanup-audio')
    def cleanup_command():
        g.app_db=app.config['DATABASE']; cleanup(); click.echo('Temporários abandonados e rascunhos expirados removidos.')

    @app.cli.command('cleanup-loop')
    @click.option('--interval', type=click.IntRange(10,300), default=60)
    def cleanup_loop(interval):
        g.app_db=app.config['DATABASE']
        click.echo('Limpeza periódica de temporários iniciada.')
        while True:
            cleanup()
            time.sleep(interval)

    return app


def initialize(app):
    with sqlite3.connect(app.config['DATABASE']) as connection:
        migrations=Path(__file__).parent.parent/'migrations'
        connection.executescript((migrations/'001_initial.sql').read_text())
        if 'solution' not in [row[1] for row in connection.execute('PRAGMA table_info(tickets)')]:
            connection.executescript((migrations/'002_ticket_resolution.sql').read_text())


def seed(app,password):
    with sqlite3.connect(app.config['DATABASE']) as connection:
        if connection.execute('SELECT count(*) FROM users').fetchone()[0]:
            raise click.ClickException('Seed permitido apenas em banco vazio; nenhum dado foi sobrescrito.')
        connection.executemany('INSERT INTO teams VALUES(?,?)',[(1,'Conectividade'),(2,'Cloud e segurança')])
        users=[(1,'Ana Martins','cliente','cliente',1),(2,'Lucas Ferreira','atendente','atendente',1),(3,'Camila Oliveira','supervisor','supervisor',1),(4,'Rafael Costa','gestor','gestor',None),(5,'Bruno Alves','cliente2','cliente',2),(6,'Beatriz Lima','atendente2','atendente',2),(7,'Marina Santos','supervisor2','supervisor',2),(8,'Clara Souza','cliente3','cliente',1),(9,'Pedro Silva','atendente3','atendente',1)]
        for uid,name,email,role,team in users:
            connection.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',(uid,name,email+'@demo.cti.local',generate_password_hash(password),role,team))
        samples=['Excelente atendimento, resolveu meu problema. Obrigada!','Estou sem internet desde ontem e vou cancelar.','Preciso de informações sobre meu plano.','Há uma cobrança indevida na minha fatura.','Atendimento rápido, ótimo suporte!','Já reclamei várias vezes e ninguém resolveu.','A instalação foi realizada conforme combinado.','Suspeita de fraude na minha conta.','Equipe excelente, obrigada pela atenção.','O atendimento foi ruim, não gostei da demora.','Meu serviço está fora do ar.','Tudo certo com a atualização do contrato.']
        for index in range(30):
            tid=index+1; other=index%3==2; alternate=index%5==4 and not other
            client,agent,team=(5,6,2) if other else (8,9,1) if alternate else (1,2,1)
            date=(datetime.now(timezone.utc)-timedelta(days=29-index)).isoformat(timespec='seconds')
            subject=['Conexão de internet','Ajuste de fatura','Suporte cloud'][index%3]
            status='encerrado' if index%2==0 else 'resolvido' if index%6==5 else 'aberto'
            solution={'Conexão de internet':'Reiniciei o equipamento remotamente e reconfigurei o sinal do roteador.','Ajuste de fatura':'Estornei a cobrança indevida e emiti a segunda via da fatura.','Suporte cloud':'Reabri o acesso à instância e orientei a troca da chave de segurança.'}[subject] if status!='aberto' else None
            connection.execute('INSERT INTO tickets(id,protocol,client_id,agent_id,team_id,subject,channel,status,created,solution,resolved_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(tid,f'AT-DEMO-{tid:04}',client,agent,team,subject,['portal','telefone','chat','email'][index%4],status,date,solution,date if solution else None,date if status=='encerrado' else None))
            text=samples[index%len(samples)]
            connection.execute('INSERT INTO feedbacks VALUES(?,?,?,?,?,?,?,?)',(tid,f'FB-DEMO-{tid:04}',tid,client,text,'texto',date,'pendente' if index%11==0 else 'analisado'))
            if index%11!=0:
                # Demo seed remains local regardless of production adapter settings.
                negative=any(s in text.lower() for s in ['cancel','indevida','reclamei','fraude','ruim','fora do ar'])
                positive=any(s in text.lower() for s in ['excelente','ótimo'])
                critical=negative and 'ruim' not in text
                topic=['conectividade','faturamento','atendimento','segurança'][index%4]
                connection.execute('INSERT INTO analyses(feedback_id,sentiment,confidence,summary,topics,critical,evidence,provider,created) VALUES(?,?,?,?,?,?,?,?,?)',(tid,'negativo' if negative else 'positivo' if positive else 'neutro',None,text,json.dumps([topic]),int(critical),json.dumps([text] if critical else []),'Demonstração · dados fictícios',date))
                if critical and index%2==0:
                    supervisor=7 if other else 3
                    connection.execute('INSERT INTO reviews(feedback_id,author_id,sentiment,critical,justification,responsible_id,status,action,created) VALUES(?,?,?,?,?,?,?,?,?)',(tid,supervisor,'negativo',1,'Revisão fictícia para demonstração.',agent,'resolvido','Contato com o cliente e correção do serviço (fictício).',date))
