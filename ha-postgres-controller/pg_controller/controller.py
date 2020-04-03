import argparse
import logging
import signal
import sys
import threading
import time

import psycopg2
import requests

from pg_controller import state
from pg_controller.checks import PostgresAliveCheck, PostgresStandbyReplicationCheck
from pg_controller.workers.election import Election, ElectionStatusHandler
from pg_controller.workers.health_monitor import HealthMonitor
from pg_controller.workers.management import ManagementServer


class PostgresMasterElectionStatusHandler(ElectionStatusHandler):
    """
    Promotes a standby database to master, by executing Postgres's 'pg_promote' sql function against the monitored
    database.
    """

    def __init__(self):
        pass

    def handle_status(self, is_leader):
        """
        Executes 'pg_promote' sql function if the election is won, and the database role is 'Standby'. It also handles
        promotion failures by setting the role to 'DeadMaster'.
        """
        if is_leader is False or state.INSTANCE.role != state.ROLE_STANDBY:
            return

        logging.info('Executing pg_promote()!')
        conn = None
        try:
            conn = psycopg2.connect(user='controller', host='localhost')
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute('SELECT pg_promote(true)')
            result = cursor.fetchall()
            if result[0][0] is not True:
                raise RuntimeError('pg_promote was not successful! (result: %s)' % result)

            state.INSTANCE.role = state.ROLE_MASTER
        except:
            logging.exception('An exception occurred during promotion!')
            state.INSTANCE.role = state.ROLE_DEAD_MASTER
        finally:
            if conn:
                conn.close()

    def continue_participating(self):
        """Returns True if the role is 'Standby'."""
        return state.INSTANCE.role == state.ROLE_STANDBY


ELECTION_CONSUL_KEY = "service/postgres/master"
worker_threads = []


def get_args():
    parser = argparse.ArgumentParser(description='Controller daemon for ha-postgres')
    parser.add_argument('--check-interval', type=int,
                        help='The time interval (in seconds) between two consecutive health/leader election checks')
    parser.add_argument('--connect-timeout', type=int, default=1,
                        help='The timeout (in seconds) for connecting to postgres during health checks')
    parser.add_argument('--alive-check-failure-threshold', type=int, default=1,
                        help='The number of consecutive failures for the alive health check to be considered failed')
    parser.add_argument('--standby-replication-check-failure-threshold', type=int, default=4,
                        help='The number of consecutive failures for the standby replication health check '
                             'to be considered failed')
    parser.add_argument('--management-port', type=int, default=80,
                        help='The port on which the controller exposes the management API')
    parser.add_argument('--pod-ip',
                        help='The ip of this pod')

    return parser.parse_args()


def set_initial_role():
    """
    Sets the role of the database. If the election key exists, then the assumed role is 'Master', otherwise 'Standby'.
    """
    while state.INSTANCE.role is None:
        logging.info("Checking whether the election key exists")
        try:
            response = requests.get(Election.CONSUL_KV_URL.format(ELECTION_CONSUL_KEY))
            logging.info("Response (%d) %s", response.status_code, response.text)
            if response.status_code == 404:
                state.INSTANCE.role = state.ROLE_MASTER
            elif response.status_code == 200:
                state.INSTANCE.role = state.ROLE_STANDBY
        except:
            logging.exception("An error occurred while sending request to local consul client!")

        time.sleep(3)


def start_alive_health_monitor(args):
    """Starts a monitoring worker thread with the alive health check."""
    health_check = PostgresAliveCheck(args.alive_check_failure_threshold, args.connect_timeout)
    health_monitor = HealthMonitor(health_check, args.check_interval)
    health_monitor.setName("AliveMonitor")
    health_monitor.start()
    worker_threads.append(health_monitor)


def start_standby_replication_health_monitor(args):
    """Starts a monitoring worker thread with the standby replication health check."""
    health_check = PostgresStandbyReplicationCheck(args.standby_replication_check_failure_threshold,
                                                   args.connect_timeout)
    health_monitor = HealthMonitor(health_check, args.check_interval)
    health_monitor.setName("ReplicationMonitor")
    health_monitor.start()
    worker_threads.append(health_monitor)


def register_consul_service():
    """Registers the 'postgres' service in Consul."""
    logging.info("Registering Consul service: postgres")
    response = requests.put(Election.CONSUL_BASE_URL + "/agent/service/register", json={"Name": "postgres"})
    logging.info("Response (%d) %s", response.status_code, response.text)
    response.raise_for_status()


def start_election(args):
    """Starts the election worker thread."""
    election = Election(election_consul_key=ELECTION_CONSUL_KEY,
                        consul_session_checks=[state.ALIVE_HEALTH_CHECK_NAME,
                                               state.STANDBY_REPLICATION_HEALTH_CHECK_NAME],
                        election_status_handler=PostgresMasterElectionStatusHandler(),
                        host_ip=args.pod_ip,
                        check_interval_seconds=args.check_interval)
    election.start()
    worker_threads.append(election)


def start_management_server(args):
    """Starts the management server worker thread."""
    management_server = ManagementServer(args.management_port)
    management_server.start()
    worker_threads.append(management_server)


def stop(*args):
    """Stops all worker threads, and waits for them to finish."""
    for worker_thread in worker_threads:
        worker_thread.stop()
        if worker_thread.is_alive():
            worker_thread.join()


def start():
    """Starts the controller process."""
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format='[%(asctime)s][%(threadName)s] %(levelname)s: %(message)s')

    signal.signal(signal.SIGTERM, stop)

    threading.current_thread().name = "Controller"
    args = get_args()

    try:
        set_initial_role()
        start_management_server(args)
        start_alive_health_monitor(args)
        start_standby_replication_health_monitor(args)
        register_consul_service()
        state.INSTANCE.wait_till_healthy()
        start_election(args)
        state.INSTANCE.done_initializing()
    except:
        logging.exception("An exception was encountered during startup!")
        stop()
