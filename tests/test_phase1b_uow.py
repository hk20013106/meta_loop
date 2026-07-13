from meta_loop.infrastructure.postgres import PostgresUnitOfWork


class Cursor:
    def __enter__(self): return self
    def __exit__(self, *args): pass


class Connection:
    def __init__(self): self.commits = self.rollbacks = 0
    def cursor(self): return Cursor()
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1
    def close(self): pass


def test_postgres_unit_of_work_commits_only_when_explicitly_requested():
    connection = Connection()
    with PostgresUnitOfWork(lambda: connection) as uow:
        assert uow.tasks._connection is connection
        assert uow.events._connection is connection
        assert uow.queue._connection is connection
        uow.commit()

    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_postgres_unit_of_work_rolls_back_when_not_committed():
    connection = Connection()
    with PostgresUnitOfWork(lambda: connection):
        pass

    assert connection.commits == 0
    assert connection.rollbacks == 1
