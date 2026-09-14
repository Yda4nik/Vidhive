def test_roles_seeded_and_admin_created(client, raw_sql):
    # The client fixture starts the app (lifespan runs the bootstrap).
    names = {r[0] for r in raw_sql("SELECT name FROM roles")}
    assert {"viewer", "operator", "administrator"} <= names
    admins = raw_sql(
        "SELECT u.username FROM users u "
        "JOIN user_roles ur ON ur.user_id=u.id "
        "JOIN roles r ON r.id=ur.role_id WHERE r.name='administrator'"
    )
    assert ("tester-admin",) in admins


def test_bootstrap_is_idempotent(client, raw_sql):
    # Only one user (the bootstrap admin) even though lifespan ran.
    n = raw_sql("SELECT COUNT(*) FROM users")[0][0]
    assert n == 1
