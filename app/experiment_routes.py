"""Authenticated experiment planning, immutable registration and one final analysis."""
import csv
import io
import json
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from . import db, experiments as ab


def router(settings, templates, current_user):
    routes = APIRouter()
    def today():
        return datetime.now(ZoneInfo(settings.timezone)).date()

    def get(session, experiment_id):
        experiment = session.get(db.ABExperiment,experiment_id)
        if experiment is None:
            raise HTTPException(404,'Forsøget findes ikke')
        return experiment

    @routes.get('/resultater/ab')
    def page(request: Request, experiment_id: int = 0, error: str = '', user: str = Depends(current_user)):
        with db.get_session() as session:
            all_experiments = session.scalars(select(db.ABExperiment).order_by(db.ABExperiment.id.desc())).all()
            experiment = get(session,experiment_id) if experiment_id else None
            protocol = json.loads(experiment.protocol) if experiment else None
            evidence = ab.evidence(session,experiment,today()) if experiment else None
            result = json.loads(experiment.final_analysis) if experiment and experiment.final_analysis else None
            start = today()+timedelta(days=31)
            start += timedelta(days=(-start.weekday())%7)
            return templates.TemplateResponse(request,'experiments.html',dict(settings=settings,user=user,
                experiments=all_experiments,experiment=experiment,protocol=protocol,evidence=evidence,result=result,
                drift=bool(protocol and ab.policy_snapshot(settings)['digest']!=protocol['model']['digest']),
                error=error,default_start=start.isoformat(),today=today().isoformat()))

    @routes.post('/resultater/ab/opret')
    async def create(request: Request, user: str = Depends(current_user)):
        with db.get_session() as session:
            try:
                # Serializes overlapping registrations with SQLite's write lock.
                if session.get_bind().dialect.name=='sqlite':
                    from sqlalchemy import text
                    session.execute(text('BEGIN IMMEDIATE'))
                experiment = ab.create(session,dict(await request.form()),settings,user,today())
                session.commit()
                return RedirectResponse(f'/resultater/ab?experiment_id={experiment.id}#forsoeg',303)
            except ValueError as exc:
                session.rollback()
                return RedirectResponse('/resultater/ab?error='+quote(str(exc))+'#opret',303)

    @routes.post('/resultater/ab/{experiment_id}/kontrol')
    async def review(experiment_id: int, request: Request, user: str = Depends(current_user)):
        with db.get_session() as session:
            experiment = get(session,experiment_id)
            try:
                ab.log_review(session,experiment,dict(await request.form()),user,today())
                session.commit()
                suffix='#forsoeg'
            except ValueError as exc:
                session.rollback()
                suffix='&error='+quote(str(exc))+'#forsoeg'
            return RedirectResponse(f'/resultater/ab?experiment_id={experiment_id}'+suffix,303)

    @routes.post('/resultater/ab/{experiment_id}/afslut')
    def finalize(experiment_id: int, attest: bool = Form(False), user: str = Depends(current_user)):
        with db.get_session() as session:
            try:
                ab.finalize(session,get(session,experiment_id),settings,user,today(),attest)
                session.commit()
                suffix='#forsoeg'
            except ValueError as exc:
                session.rollback()
                suffix='&error='+quote(str(exc))+'#forsoeg'
            return RedirectResponse(f'/resultater/ab?experiment_id={experiment_id}'+suffix,303)

    @routes.post('/resultater/ab/{experiment_id}/stop')
    def stop(experiment_id: int, note: str = Form(''), user: str = Depends(current_user)):
        with db.get_session() as session:
            try:
                ab.stop(session,get(session,experiment_id),note,user)
                session.commit()
                suffix='#forsoeg'
            except ValueError as exc:
                session.rollback()
                suffix='&error='+quote(str(exc))+'#forsoeg'
            return RedirectResponse(f'/resultater/ab?experiment_id={experiment_id}'+suffix,303)

    @routes.get('/resultater/ab/{experiment_id}/protokol.json')
    def export_protocol(experiment_id: int, user: str = Depends(current_user)):
        with db.get_session() as session:
            experiment = get(session,experiment_id)
            logs = session.scalars(select(db.ABLog).where(db.ABLog.experiment_id==experiment_id).order_by(db.ABLog.id)).all()
            payload = dict(id=experiment.id,name=experiment.name,actor=experiment.actor,
                created_at=experiment.created_at.isoformat()+'Z',status=experiment.status,
                sha256=experiment.digest,protocol=json.loads(experiment.protocol),
                final_analysis=json.loads(experiment.final_analysis) if experiment.final_analysis else None,
                log=[dict(id=l.id,kind=l.kind,actor=l.actor,created_at=l.created_at.isoformat()+'Z',
                          payload=json.loads(l.payload)) for l in logs])
        return Response(ab.dumps(payload),media_type='application/json',headers={
            'Content-Disposition':f'attachment; filename="ab-{experiment_id}-protokol.json"'})

    @routes.get('/resultater/ab/{experiment_id}/kalender.csv')
    def export_calendar(experiment_id: int, user: str = Depends(current_user)):
        with db.get_session() as session:
            p = json.loads(get(session,experiment_id).protocol)
        buffer = io.StringIO()
        writer = csv.writer(buffer,delimiter=';')
        writer.writerow(['forsoeg','par','blok','opholdsdato','strategi','prisstrategi_fra','prisstrategi_til','kapacitet_fast'])
        for i,block in enumerate(p['blocks'],1):
            for day in ab.block_dates(block):
                writer.writerow([experiment_id,block['pair'],i,day,block['arm'],day-timedelta(days=p['lead']),day,p['capacity']])
        return Response(buffer.getvalue().encode('utf-8-sig'),media_type='text/csv',headers={
            'Content-Disposition':f'attachment; filename="ab-{experiment_id}-kalender.csv"'})
    return routes
