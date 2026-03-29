import os, sqlite3, secrets, random
from flask import Flask, session, redirect, request, jsonify, send_from_directory
import requests

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

DISCORD_CLIENT_ID     = os.environ.get('DISCORD_CLIENT_ID', '1486408962307002631')
DISCORD_CLIENT_SECRET = os.environ.get('DISCORD_CLIENT_SECRET', '')
DISCORD_REDIRECT_URI  = os.environ.get('DISCORD_REDIRECT_URI', 'https://casinow.one/callback')
DB_PATH               = os.environ.get('DB_PATH', '/data/casino.db')
DISCORD_API           = 'https://discord.com/api/v10'

# ── DB ─────────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_user(uid):
    db = get_db()
    db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
    db.commit(); db.close()

def get_balance(uid):
    db = get_db()
    r = db.execute("SELECT balance FROM users WHERE user_id=?", (uid,)).fetchone()
    db.close()
    if r: return r['balance']
    ensure_user(uid); return 1000

def update_balance(uid, amount):
    db = get_db()
    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
    db.commit(); db.close()

def record_win(uid, amount):
    db = get_db()
    db.execute("UPDATE users SET total_won=total_won+? WHERE user_id=?", (amount, uid))
    db.commit(); db.close()

def record_loss(uid, amount):
    db = get_db()
    db.execute("UPDATE users SET total_lost=total_lost+? WHERE user_id=?", (amount, uid))
    db.commit(); db.close()

def get_stats(uid):
    db = get_db()
    r = db.execute("SELECT balance,bank,total_won,total_lost FROM users WHERE user_id=?", (uid,)).fetchone()
    db.close()
    if r: return dict(r)
    return {'balance':1000,'bank':0,'total_won':0,'total_lost':0}

# ── AUTH ───────────────────────────────────────────────────────────────────────
from functools import wraps
def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if 'user_id' not in session:
            return jsonify({'error':'unauthorized'}), 401
        return f(*a, **kw)
    return dec

@app.route('/login')
def login():
    state = secrets.token_hex(16)
    session['state'] = state
    url = (f"{DISCORD_API}/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
           f"&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code"
           f"&scope=identify&state={state}")
    return redirect(url)

@app.route('/callback')
def callback():
    code  = request.args.get('code')
    state = request.args.get('state')
    if not code or state != session.get('state'):
        return redirect('/')
    r = requests.post(f"{DISCORD_API}/oauth2/token", data={
        'client_id': DISCORD_CLIENT_ID, 'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code', 'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI,
    })
    if r.status_code != 200: return redirect('/')
    token = r.json().get('access_token')
    u = requests.get(f"{DISCORD_API}/users/@me", headers={'Authorization':f'Bearer {token}'}).json()
    session['user_id']  = int(u['id'])
    session['username'] = u['username']
    session['avatar']   = u.get('avatar','')
    ensure_user(int(u['id']))
    return redirect('/')

@app.route('/logout')
def logout():
    session.clear(); return redirect('/')

# ── API ────────────────────────────────────────────────────────────────────────
@app.route('/api/me')
@login_required
def api_me():
    uid = session['user_id']
    stats = get_stats(uid)
    av = (f"https://cdn.discordapp.com/avatars/{uid}/{session['avatar']}.png"
          if session.get('avatar')
          else "https://cdn.discordapp.com/embed/avatars/0.png")
    return jsonify({'username':session['username'],'avatar':av,**stats})

# PLINKO
PLINKO_MULTI = {
    8:  [5.6,2.1,1.1,1.0,0.5,1.0,1.1,2.1,5.6],
    12: [10,3,1.6,1.4,1.1,1.0,0.5,1.0,1.1,1.4,1.6,3,10],
    16: [16,9,2,1.4,1.4,1.2,1.1,1.0,0.5,1.0,1.1,1.2,1.4,1.4,2,9,16],
}
@app.route('/api/plinko', methods=['POST'])
@login_required
def api_plinko():
    uid = session['user_id']
    d   = request.json
    bet = int(d.get('bet',0)); rows = int(d.get('rows',16))
    if bet<=0: return jsonify({'error':'Mise invalide'}),400
    if bet>get_balance(uid): return jsonify({'error':'Solde insuffisant'}),400
    if rows not in PLINKO_MULTI: rows=16
    path=[]; pos=0
    for _ in range(rows):
        go=random.randint(0,1); path.append(go); pos+=go
    mults=PLINKO_MULTI[rows]; slot=min(pos,len(mults)-1)
    mult=mults[slot]; win=int(bet*mult); profit=win-bet
    update_balance(uid,profit)
    if profit>0: record_win(uid,profit)
    else: record_loss(uid,abs(profit))
    return jsonify({'path':path,'slot':slot,'multiplier':mult,'winnings':win,'profit':profit,'balance':get_balance(uid)})

# MINES
_mines_sessions={}
@app.route('/api/mines/start', methods=['POST'])
@login_required
def mines_start():
    uid=session['user_id']; d=request.json
    bet=int(d.get('bet',0)); n=int(d.get('mines',5))
    if bet<=0: return jsonify({'error':'Mise invalide'}),400
    if bet>get_balance(uid): return jsonify({'error':'Solde insuffisant'}),400
    if not 1<=n<=24: return jsonify({'error':'Mines invalide'}),400
    update_balance(uid,-bet)
    pos=list(range(25)); random.shuffle(pos)
    _mines_sessions[uid]={'bet':bet,'mines':set(pos[:n]),'revealed':set(),'n':n,'active':True}
    return jsonify({'success':True,'balance':get_balance(uid)})

@app.route('/api/mines/reveal', methods=['POST'])
@login_required
def mines_reveal():
    uid=session['user_id']; d=request.json; pos=int(d.get('pos',-1))
    gs=_mines_sessions.get(uid)
    if not gs or not gs['active']: return jsonify({'error':'Pas de partie'}),400
    if pos in gs['revealed']: return jsonify({'error':'Deja revele'}),400
    gs['revealed'].add(pos)
    if pos in gs['mines']:
        gs['active']=False; mines=list(gs['mines'])
        record_loss(uid,gs['bet']); del _mines_sessions[uid]
        return jsonify({'hit':True,'mines':mines,'balance':get_balance(uid)})
    rev=len(gs['revealed']); total=25; n=gs['n']
    mult=0.97
    for i in range(rev): mult/=(total-n-i)/(total-i)
    mult=round(mult,2); pot=int(gs['bet']*mult)
    return jsonify({'hit':False,'multiplier':mult,'potential':pot,'balance':get_balance(uid)})

@app.route('/api/mines/cashout', methods=['POST'])
@login_required
def mines_cashout():
    uid=session['user_id']; gs=_mines_sessions.get(uid)
    if not gs or not gs['active']: return jsonify({'error':'Pas de partie'}),400
    rev=len(gs['revealed']); total=25; n=gs['n']
    if rev==0: update_balance(uid,gs['bet']); del _mines_sessions[uid]; return jsonify({'winnings':gs['bet'],'balance':get_balance(uid)})
    mult=0.97
    for i in range(rev): mult/=(total-n-i)/(total-i)
    mult=round(mult,2); win=int(gs['bet']*mult); profit=win-gs['bet']
    update_balance(uid,win)
    if profit>0: record_win(uid,profit)
    gs['active']=False; del _mines_sessions[uid]
    return jsonify({'winnings':win,'multiplier':mult,'balance':get_balance(uid)})

# SLOTS
SYMS=['🍒','🍋','🔔','⭐','💎','7️⃣']; WTS=[30,25,20,15,7,3]
PAYS={'7️⃣':50,'💎':20,'⭐':10,'🔔':5,'🍋':3,'🍒':2}
def wsym():
    t=sum(WTS); r=random.uniform(0,t); u=0
    for s,w in zip(SYMS,WTS):
        u+=w
        if r<=u: return s
    return SYMS[-1]
@app.route('/api/slots', methods=['POST'])
@login_required
def api_slots():
    uid=session['user_id']; d=request.json; bet=int(d.get('bet',0))
    if bet<=0: return jsonify({'error':'Mise invalide'}),400
    if bet>get_balance(uid): return jsonify({'error':'Solde insuffisant'}),400
    reels=[[wsym() for _ in range(3)] for _ in range(3)]
    mid=[reels[i][1] for i in range(3)]
    if mid[0]==mid[1]==mid[2]: win=int(bet*PAYS.get(mid[0],1))
    elif mid[0]==mid[1] or mid[1]==mid[2]: win=int(bet*1.5)
    else: win=0
    profit=win-bet; update_balance(uid,profit)
    if profit>0: record_win(uid,profit)
    else: record_loss(uid,abs(profit))
    return jsonify({'reels':reels,'middle':mid,'winnings':win,'profit':profit,'balance':get_balance(uid)})

# COINFLIP
@app.route('/api/coinflip', methods=['POST'])
@login_required
def api_coinflip():
    uid=session['user_id']; d=request.json
    bet=int(d.get('bet',0)); choice=d.get('choice','heads')
    if bet<=0: return jsonify({'error':'Mise invalide'}),400
    if bet>get_balance(uid): return jsonify({'error':'Solde insuffisant'}),400
    res=random.choice(['heads','tails']); won=res==choice
    profit=bet if won else -bet; update_balance(uid,profit)
    if won: record_win(uid,bet)
    else: record_loss(uid,bet)
    return jsonify({'result':res,'won':won,'profit':profit,'balance':get_balance(uid)})

# BLACKJACK
_bj_sessions={}
def card_value(hand):
    vals={'2':2,'3':3,'4':4,'5':5,'6':6,'7':7,'8':8,'9':9,'10':10,'J':10,'Q':10,'K':10,'A':11}
    total=sum(vals[c] for c in hand); aces=hand.count('A')
    while total>21 and aces: total-=10; aces-=1
    return total
def draw(deck): return deck.pop()
def new_deck():
    ranks=['2','3','4','5','6','7','8','9','10','J','Q','K','A']*4
    random.shuffle(ranks); return ranks

@app.route('/api/blackjack/start', methods=['POST'])
@login_required
def bj_start():
    uid=session['user_id']; d=request.json; bet=int(d.get('bet',0))
    if bet<=0: return jsonify({'error':'Mise invalide'}),400
    if bet>get_balance(uid): return jsonify({'error':'Solde insuffisant'}),400
    update_balance(uid,-bet)
    deck=new_deck()
    player=[draw(deck),draw(deck)]; dealer=[draw(deck),draw(deck)]
    _bj_sessions[uid]={'bet':bet,'deck':deck,'player':player,'dealer':dealer,'active':True}
    pv=card_value(player); dv=card_value(dealer)
    bj=False
    if pv==21:
        bj=True; win=int(bet*2.5); update_balance(uid,win); record_win(uid,win-bet)
        del _bj_sessions[uid]
        return jsonify({'player':player,'dealer':dealer,'player_val':pv,'dealer_val':dv,'blackjack':True,'winnings':win,'balance':get_balance(uid)})
    return jsonify({'player':player,'dealer':[dealer[0],'?'],'player_val':pv,'dealer_val_shown':card_value([dealer[0]]),'active':True,'balance':get_balance(uid)})

@app.route('/api/blackjack/hit', methods=['POST'])
@login_required
def bj_hit():
    uid=session['user_id']; gs=_bj_sessions.get(uid)
    if not gs or not gs['active']: return jsonify({'error':'Pas de partie'}),400
    gs['player'].append(draw(gs['deck'])); pv=card_value(gs['player'])
    if pv>21:
        gs['active']=False; record_loss(uid,gs['bet']); del _bj_sessions[uid]
        return jsonify({'player':gs['player'],'player_val':pv,'bust':True,'balance':get_balance(uid)})
    return jsonify({'player':gs['player'],'player_val':pv,'active':True,'balance':get_balance(uid)})

@app.route('/api/blackjack/stand', methods=['POST'])
@login_required
def bj_stand():
    uid=session['user_id']; gs=_bj_sessions.get(uid)
    if not gs or not gs['active']: return jsonify({'error':'Pas de partie'}),400
    while card_value(gs['dealer'])<17: gs['dealer'].append(draw(gs['deck']))
    pv=card_value(gs['player']); dv=card_value(gs['dealer']); gs['active']=False
    if dv>21 or pv>dv: win=gs['bet']*2; update_balance(uid,win); record_win(uid,gs['bet']); result='win'
    elif pv==dv: update_balance(uid,gs['bet']); result='push'
    else: record_loss(uid,gs['bet']); result='lose'
    win_amt = gs['bet']*2 if result=='win' else (gs['bet'] if result=='push' else 0)
    del _bj_sessions[uid]
    return jsonify({'player':gs['player'],'dealer':gs['dealer'],'player_val':pv,'dealer_val':dv,'result':result,'winnings':win_amt,'balance':get_balance(uid)})

# ── PAGES ──────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('templates','index.html')

@app.route('/game/<name>')
def game_page(name):
    valid=['plinko','mines','slots','coinflip','blackjack']
    if name not in valid: return redirect('/')
    return send_from_directory('templates', name+'.html')

@app.route('/static/<path:p>')
def static_files(p):
    return send_from_directory('static',p)

if __name__=='__main__':
    port=int(os.environ.get('PORT',5000))
    app.run(host='0.0.0.0',port=port,debug=False)
