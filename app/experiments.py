"""Prospective paired-block policy experiments, separate from price exploration.

Randomization unit: entire property's stay-date block. All assigned dates remain
in ITT. No interim inferential output, outcome imputation or post-hoc exclusions.
"""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from importlib.metadata import version
from statistics import NormalDist, mean, stdev

import numpy as np
from scipy.stats import t as student_t
from sqlalchemy import select, update

from . import db, validation, level

ROOT = Path(__file__).resolve().parents[1]
ALPHA = .05
POWER = .8
DRAWS = 99999


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def analysis_method():
    return dict(name='paired-block-v1',source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                numpy=np.__version__,scipy=version('scipy'))


def policy_snapshot(settings):
    parameters = asdict(settings.params)
    # Read the level actually consumed by the runtime, not cached startup values.
    path = Path(settings.params.v4.level_path)
    state = level.load(path if path.is_absolute() else ROOT/path)
    parameters['demand_level'] = [state.rooms,state.beds]
    files = {p: hashlib.sha256((ROOT / 'app' / p).read_bytes()).hexdigest() for p in
             ('engine.py', 'bidprice.py', 'ladder.py', 'demand.py', 'level.py', 'service.py',
              'config.py', 'pricing_basis.py', 'uncertainty.py')}
    payload = dict(parameters=parameters, files=files, timezone=settings.timezone,
                   horizon_days=settings.horizon_days)
    return dict(payload=payload, digest=hashlib.sha256(dumps(payload).encode()).hexdigest())


def positive(value, label):
    try:
        number = float(str(value).replace(',', '.'))
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{label} skal være et positivt tal') from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'{label} skal være et positivt, endeligt tal')
    return number


def finite_number(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def power_pairs(sigma, delta):
    """Normal planning approximation for the SD OF PAIRED BLOCK DIFFERENCES."""
    z = NormalDist().inv_cdf(1-ALPHA/2) + NormalDist().inv_cdf(POWER)
    ratio = z*sigma/delta
    if not math.isfinite(ratio) or ratio>math.sqrt(52):
        raise ValueError('Designet kræver over 52 par; revurdér varighed og dokumenterede forudsætninger')
    return max(6, math.ceil(ratio**2))


def schedule(start, pairs, days, seed):
    rng = np.random.Generator(np.random.PCG64(seed))
    rows = []
    for pair in range(1, pairs+1):
        arms = ('A', 'B') if rng.integers(2) == 0 else ('B', 'A')
        for slot, arm in enumerate(arms):
            first = start + timedelta(days=((pair-1)*2+slot)*days)
            rows.append(dict(pair=pair, slot=slot+1, arm=arm,
                             start=first.isoformat(), end=(first+timedelta(days=days-1)).isoformat()))
    return rows


def create(session, raw, settings, actor, today):
    if settings.params.v4.level_alpha != 0:
        raise ValueError('Den fælles niveaulæring skal pauses før præregistrering; ellers kobler tidligere blokke sig til senere priser')
    name = str(raw.get('name', '')).strip()
    baseline = str(raw.get('baseline', '')).strip()
    safeguards = str(raw.get('safeguards', '')).strip()
    interference = str(raw.get('interference', '')).strip()
    if not name or len(name) > 160 or any(len(v) < 30 or len(v) > 6000 for v in (baseline, safeguards, interference)):
        raise ValueError('Angiv et navn og mindst 30 tegn om kontrolstrategi, stopregler og risiko for afsmitning')
    try:
        start = date.fromisoformat(raw.get('start', ''))
        pairs, days, lead, capacity = (int(raw.get(k, 0)) for k in ('pairs', 'block_days', 'lead', 'capacity'))
    except (ValueError, TypeError) as exc:
        raise ValueError('Kontrollér startdato, antal par, bloklængde, prisperiode og kapacitet') from exc
    if days not in (7, 14, 28) or not 6 <= pairs <= 52 or not 1 <= lead <= 120 or not 1 <= capacity <= 10000:
        raise ValueError('Vælg 6–52 par, blokke på 7/14/28 dage, 1–120 dages prisperiode og positiv kapacitet')
    if start.weekday() != 0 or start - timedelta(days=lead) <= today:
        raise ValueError('Første opholdsdato skal være en mandag; prisperioden skal begynde efter i dag')
    sigma = positive(raw.get('sigma'), 'Standardafvigelse mellem par')
    delta = positive(raw.get('delta'), 'Mindste relevante forbedring')
    required = power_pairs(sigma, delta)
    if pairs < required:
        raise ValueError(f'Planlægningsskønnet kræver mindst {required} par; ændr designet eller begrundede forudsætninger')
    if not raw.get('understood'):
        raise ValueError('Bekræft, at protokollen låses, og priserne skal anvendes manuelt efter kalenderen')
    last = start+timedelta(days=2*pairs*days-1)
    for old in session.scalars(select(db.ABExperiment)):
        protocol = json.loads(old.protocol)
        if start.isoformat() <= protocol['end'] and last.isoformat() >= protocol['start']:
            raise ValueError('Opholdsdatoerne overlapper et allerede registreret forsøg, også hvis det er stoppet')
    seed = secrets.randbits(63)
    protocol = dict(version=1, method=analysis_method(), start=start.isoformat(), end=last.isoformat(), pairs=pairs,
        block_days=days, lead=lead, capacity=capacity, sigma=sigma, delta=delta,
        required_pairs=required, alpha=ALPHA, power=POWER, primary='contribution_per_fixed_bednight',
        baseline=baseline, safeguards=safeguards, interference=interference,
        model=policy_snapshot(settings), seed=seed, blocks=schedule(start,pairs,days,seed),
        inference_seed=secrets.randbits(63), mc_draws=DRAWS,
        learning='Niveaulæring pauset; startniveau, træning, kode og parametre låst',
        exclusions='Ingen opholdsdatoer, grupper eller events fjernes efter randomisering')
    text = dumps(protocol)
    experiment = db.ABExperiment(name=name, actor=actor, protocol=text,
        digest=hashlib.sha256(text.encode()).hexdigest(), status='collecting')
    session.add(experiment)
    session.flush()
    db.log(session, 'ab_preregister', actor=actor, detail=f'Forsøg {experiment.id}; {experiment.digest}')
    return experiment


def block_dates(block):
    start, end = (date.fromisoformat(block[k]) for k in ('start', 'end'))
    return [start+timedelta(days=i) for i in range((end-start).days+1)]


def evidence(session, experiment, today):
    """Latest outcome record, including a later withdrawal of final confirmation."""
    p = json.loads(experiment.protocol)
    records = session.scalars(select(db.ValidationRecord).where(db.ValidationRecord.kind=='outcome',
        db.ValidationRecord.day.between(date.fromisoformat(p['start']),date.fromisoformat(p['end'])))
        .order_by(db.ValidationRecord.recorded_at,db.ValidationRecord.id)).all()
    latest = {r.day:r for r in records}
    reviews = {}
    logs = session.scalars(select(db.ABLog).where(db.ABLog.experiment_id==experiment.id)
        .order_by(db.ABLog.id)).all()
    for log in logs:
        payload = json.loads(log.payload)
        if log.kind=='block_review':
            reviews[payload['block']] = dict(payload, id=log.id)
    rows, sources, missing = [], [], []
    for index, block in enumerate(p['blocks']):
        amounts, rooms, beds = [], [], []
        absent = []
        for day in block_dates(block):
            rec = latest.get(day)
            data = json.loads(rec.payload) if rec else {}
            required = ('room_revenue','bed_revenue','commission','variable_cost','final_rooms','final_beds')
            valid = (day < today and data.get('final') and data.get('revenue_date_confirmed')
                     and all(finite_number(data.get(k)) for k in required)
                     and all(data[k]>=0 for k in ('commission','variable_cost','final_rooms','final_beds'))
                     and finite_number(data.get('revenue_adjustment',0)))
            amount = validation.actual_revenue(data)-data['commission']-data['variable_cost'] if valid else None
            valid = valid and finite_number(amount)
            if not valid:
                absent.append(day.isoformat())
                continue
            amounts.append(amount)
            rooms.append(data['final_rooms']); beds.append(data['final_beds'])
            sources.append(dict(day=day.isoformat(),record_id=rec.id,payload=data,
                                source=rec.source,observed_at=rec.observed_at.isoformat()))
        missing.extend(absent)
        complete = not absent
        rows.append(dict(block, index=index, complete=complete, missing=absent, review=reviews.get(index),
            value=sum(amounts)/(p['block_days']*p['capacity']) if complete else None,
            rooms=sum(rooms) if complete else None, beds=sum(beds) if complete else None))
    return dict(blocks=rows,sources=sources,missing=missing,logs=logs,
                reviewed=sum(b['review'] is not None for b in rows),
                completed=sum(b['complete'] for b in rows), total=len(rows))


def randomization_p(differences, seed, draws=DRAWS):
    """Two-sided sign-flip test of Fisher's sharp null; equality is in the tail."""
    observed = abs(sum(differences))
    tolerance = 1e-10 * max(1.,sum(abs(d) for d in differences))
    if len(differences) <= 16:
        sums = np.array([0.])
        for d in differences:
            sums = np.concatenate((sums+d,sums-d))
        return float(np.mean(np.abs(sums)>=observed-tolerance)), 'exact', len(sums)
    rng = np.random.Generator(np.random.PCG64(seed))
    extreme = 0
    for first in range(0, draws, 5000):
        size = min(5000,draws-first)
        signs = rng.integers(0,2,(size,len(differences)))*2-1
        extreme += int(np.count_nonzero(np.abs(signs @ np.asarray(differences))>=observed-tolerance))
    return (extreme+1)/(draws+1), 'monte_carlo', draws


def analyse(blocks, protocol):
    differences, details = [], []
    for pair in range(1,protocol['pairs']+1):
        arms = {b['arm']:b for b in blocks if b['pair']==pair}
        d = arms['B']['value']-arms['A']['value']
        differences.append(d)
        details.append(dict(pair=pair, A=arms['A']['value'], B=arms['B']['value'], difference=d))
    n = len(differences)
    effect = mean(differences)
    sd = stdev(differences)
    se = sd/math.sqrt(n)
    # A zero variance sample does not justify a zero-width population CI.
    interval = None if se == 0 else [effect-student_t.ppf(.975,n-1)*se,effect+student_t.ppf(.975,n-1)*se]
    p, method, draws = randomization_p(differences,protocol['inference_seed'],protocol['mc_draws'])
    control = mean(d['A'] for d in details)
    return dict(n=n,effect=effect,sd=sd,se=se,ci=interval,p=p,p_method=method,draws=draws,
        relative=100*effect/control if control>0 else None, control=control,
        treatment=mean(d['B'] for d in details), pairs=details,
        practically_positive=bool(interval and interval[0]>protocol['delta'] and p<=ALPHA),
        positive=bool(interval and interval[0]>0 and p<=ALPHA))


def log_review(session, experiment, raw, actor, today):
    if experiment.status!='collecting':
        raise ValueError('Forsøget er afsluttet; registreringer kan ikke ændres')
    p = json.loads(experiment.protocol)
    try:
        index = int(raw.get('block',-1))
    except (ValueError,TypeError) as exc:
        raise ValueError('Ukendt blok') from exc
    if not 0<=index<len(p['blocks']) or date.fromisoformat(p['blocks'][index]['end'])>=today:
        raise ValueError('Blokken skal være afsluttet før den kan kontrolleres')
    note = str(raw.get('note','')).strip()
    adherence = raw.get('adherence')
    if adherence not in ('yes','deviations') or len(note)<20 or len(note)>6000:
        raise ValueError('Angiv om strategien blev fulgt og mindst 20 tegn om anvendelse, afvigelser og kilde')
    log = db.ABLog(experiment_id=experiment.id,actor=actor,kind='block_review',
        payload=dumps(dict(block=index,adherence=adherence,note=note)))
    session.add(log)
    db.log(session,'ab_block_review',actor=actor,detail=f'Forsøg {experiment.id}, blok {index+1}')


def finalize(session, experiment, settings, actor, today, attest=False):
    if experiment.status!='collecting':
        raise ValueError('Forsøget er allerede afsluttet')
    p = json.loads(experiment.protocol)
    if not attest:
        raise ValueError('Bekræft datagrundlag, stabile strategier og fravær af væsentlig afsmitning')
    if p['method']!=analysis_method():
        raise ValueError('Analysemetoden eller statistikbibliotekerne er ændret siden præregistreringen')
    if policy_snapshot(settings)['digest']!=p['model']['digest']:
        raise ValueError('Modelgrundlaget er ændret siden registreringen; stop forsøget og undersøg ændringen')
    e = evidence(session,experiment,today)
    if e['missing'] or e['reviewed']!=e['total']:
        raise ValueError('Alle planlagte datoer kræver afsluttede, afstemte økonomital og alle blokke en driftskontrol')
    result = analyse(e['blocks'],p)
    result.update(sources=e['sources'],blocks=e['blocks'],protocol_digest=experiment.digest,
                  finalized_at=db.utcnow().isoformat()+'Z',actor=actor,assumptions_attested=True,
                  deviations=sum(b['review']['adherence']=='deviations' for b in e['blocks']))
    changed = session.execute(update(db.ABExperiment).where(db.ABExperiment.id==experiment.id,
        db.ABExperiment.status=='collecting').values(status='finalized',final_analysis=dumps(result)))
    if changed.rowcount!=1:
        raise ValueError('Forsøget blev afsluttet i en anden session')
    session.refresh(experiment)
    db.log(session,'ab_finalize',actor=actor,detail=f'Forsøg {experiment.id}; kilder låst')
    return result


def stop(session, experiment, note, actor):
    if len(note.strip())<20 or len(note)>6000:
        raise ValueError('Angiv mindst 20 tegn om hvorfor forsøget stoppes')
    changed = session.execute(update(db.ABExperiment).where(db.ABExperiment.id==experiment.id,
        db.ABExperiment.status=='collecting').values(status='stopped'))
    if changed.rowcount!=1:
        raise ValueError('Forsøget er allerede afsluttet')
    session.refresh(experiment)
    session.add(db.ABLog(experiment_id=experiment.id,actor=actor,kind='stop',payload=dumps(dict(note=note))))
    db.log(session,'ab_stop',actor=actor,detail=f'Forsøg {experiment.id}; ingen bekræftende test')
