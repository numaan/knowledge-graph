def test_disk_full_is_a_known_fault_type():
    from app.simulation.faults import FAULT_TYPES
    assert "disk-full" in FAULT_TYPES
    assert len(FAULT_TYPES) == len(set(FAULT_TYPES))


def test_disk_full_targets_like_other_faults():
    from app.simulation.faults import FaultInput, is_targeted
    node = {"id": "db1", "type": "aws.rds", "category": "database"}
    assert is_targeted(FaultInput(type="disk-full", target_id="db1"), node)
    assert not is_targeted(FaultInput(type="disk-full", target_id="other"), node)
