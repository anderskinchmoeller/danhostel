"""Chart-ready dated diagnostics. No retroactive financial checkpoints."""
import json
from datetime import date
from functools import lru_cache
from sqlalchemy import select
from . import db, economics, forecast_validation as forecasts, forecast_metrics as metrics

MEASURES = {'rum': ('Værelser','værelser'), 'senge': ('Senge','senge'), 'revenue': ('Logiomsætning','kr.')}


@lru_cache(maxsize=2)
def read_history(mtime):
    data=json.loads(forecasts.OBSERVATIONS_PATH.read_text())
    report=json.loads(forecasts.REPORT_PATH.read_text())
    if data['model_hash'] != report['model_hash']:
        return []
    return data['rows']


def historical_rows():
    if not forecasts.OBSERVATIONS_PATH.exists() or not forecasts.REPORT_PATH.exists():
        return []
    return read_history((forecasts.OBSERVATIONS_PATH.stat().st_mtime_ns, forecasts.REPORT_PATH.stat().st_mtime_ns))


def live_rows(session, today):
    rows, _, _=forecasts.live_rows(session,today)
    finals={r['day']:r for r in economics.final_results(
        session,date(2000,1,1),today,today)}
    pending=0
    for snapshot in session.scalars(select(db.ForecastSnapshot).where(
            db.ForecastSnapshot.product=='rum',db.ForecastSnapshot.day<today)):
        actual=finals.get(snapshot.day)
        payload=json.loads(snapshot.payload)
        financial=payload.get('revenue_checkpoint')
        if actual is None or actual['revenue'] is None or actual.get('overview_only'):
            continue
        if financial is None:
            pending+=1
            continue
        expected=financial.get('expected_ex_vat')
        if expected is None or not actual['data'].get('revenue_date_confirmed'):
            pending+=1
            continue
        rows.append(dict(day=snapshot.day.isoformat(),lead=snapshot.lead,product='revenue',
            forecast=expected,actual=actual['revenue'],baseline=None,intervals={},
            run_id=snapshot.run_id,observed_at=snapshot.observed_at.isoformat()+'Z',
            capacity=0,weekend=payload['weekend'],groups=None,censored=None,
            revenue_basis=financial['basis']))
    return rows,pending


def chart(rows):
    if not rows:
        return None
    rows=sorted(rows,key=lambda r:r['day'])
    days=[date.fromisoformat(r['day']) for r in rows]
    first,last=days[0].toordinal(),days[-1].toordinal()
    errors=[r['forecast']-r['actual'] for r in rows]
    low,high=min([0]+errors),max([0]+errors)
    margin=max((high-low)*.12,1)
    low-=margin;high+=margin
    y=lambda v: 25+(high-v)/(high-low)*240
    x=lambda d: 70+(d-first)/max(1,last-first)*800
    points=[dict(row, x=x(d.toordinal()), y=y(e), error=e,
                 positive=e>0) for row,d,e in zip(rows,days,errors)]
    ticks=[dict(y=y(low+(high-low)*i/4),value=low+(high-low)*i/4) for i in range(5)]
    return dict(points=points,zero=y(0),ticks=ticks,first=days[0].strftime('%d-%m-%Y'),
                last=days[-1].strftime('%d-%m-%Y'))


def diagnostics(rows, first, last, product, lead):
    scoped=[r for r in rows if first.isoformat()<=r['day']<=last.isoformat() and r['product'] in MEASURES]
    summaries=[]
    for key in MEASURES:
        for horizon in forecasts.CHECKPOINTS:
            group=[r for r in scoped if r['product']==key and r['lead']==horizon]
            summaries.append(dict(metrics.summary(group,horizon),product=key,lead=horizon))
    metrics.adjust(summaries)
    chosen=next(s for s in summaries if s['product']==product and s['lead']==lead)
    selected=sorted([r for r in scoped if r['product']==product and r['lead']==lead],key=lambda r:r['day'])
    return dict(summary=chosen,chart=chart(selected),rows=selected)
