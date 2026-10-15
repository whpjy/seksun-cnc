import pytest
from app.bar_holding import BarHolding,extend_stock,stock_signature
from test_l32_operation_trial import job,operation,rotational,snapshot
from app.agent.l32_operation_trial import evidence_context_signature,reconcile_state,operation_signature

def test_bar_is_part_of_initial_blank_and_old_evidence_is_invalidated():
    cut=operation('OP30',30,'turn_cutoff');cut.parameters.update(z_mm=-32,cutting_width_mm=2)
    source=job(cut);source.plan.stock.update(z_min_mm=-34,z_max_mm=2,length_mm=36)
    before=evidence_context_signature(source,rotational(),snapshot())
    request=BarHolding(operation_id=cut.id,grip_length_mm=10,clamp_confirmed=True,verified_by='test',evidence='synthetic only',expected_stock_signature=stock_signature(source.plan.stock))
    source.plan.stock=extend_stock(source.plan.stock,cut,request)
    assert source.plan.stock['z_min_mm']==-43 and source.plan.stock['length_mm']==45
    assert extend_stock(source.plan.stock,cut,request)['z_min_mm']==-43
    after=evidence_context_signature(source,rotational(),snapshot());assert after!=before
    state={'schema_version':'1.1.0','context_signature':before,'accepted':[{'operation_id':cut.id,'operation_signature':operation_signature(cut),'cumulative_simulation':{}}]}
    assert reconcile_state(source,state,after)['accepted']==[]

def test_bar_length_limit_and_missing_confirmation_basis():
    cut=operation('OP30',30,'turn_cutoff');cut.parameters.update(z_mm=-200,cutting_width_mm=2)
    request=BarHolding(operation_id=cut.id,grip_length_mm=200,verified_by='test',evidence='synthetic',expected_stock_signature='x')
    with pytest.raises(ValueError,match='长度范围'):extend_stock({'z_min_mm':-202,'z_max_mm':2},cut,request)
    request.evidence=' '
    with pytest.raises(ValueError,match='依据'):extend_stock({'z_min_mm':-202,'z_max_mm':2},cut,request)
