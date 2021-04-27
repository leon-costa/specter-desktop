""" Stuff to control a bitcoind-instance.
"""
import atexit
import logging
import os
import signal
import psutil
import shutil
import subprocess
import tempfile
import time
import json
import platform


from ..util.shell import which
from ..rpc import RpcError
from ..rpc import BitcoinRPC
from ..helpers import load_jsons
from ..specter_error import ExtProcTimeoutException
from urllib3.exceptions import NewConnectionError, MaxRetryError
from requests.exceptions import ConnectionError

logger = logging.getLogger(__name__)


class Btcd_conn:
    """ An object to easily store connection data to bitcoind-comatible nodes (Bitcoin/Elements) """

    def __init__(
        self, rpcuser="bitcoin", rpcpassword="secret", rpcport=18543, ipaddress=None
    ):
        self.rpcport = rpcport
        self.rpcuser = rpcuser
        self.rpcpassword = rpcpassword
        self._ipaddress = ipaddress

    @property
    def ipaddress(self):
        if self._ipaddress == None:
            raise Exception("ipadress is none")
        else:
            return self._ipaddress

    @ipaddress.setter
    def ipaddress(self, ipaddress):
        self._ipaddress = ipaddress

    def get_rpc(self):
        """ returns a BitcoinRPC """
        # def __init__(self, user, passwd, host="127.0.0.1", port=8332, protocol="http", path="", timeout=30, **kwargs):
        rpc = BitcoinRPC(
            self.rpcuser, self.rpcpassword, host=self.ipaddress, port=self.rpcport
        )
        rpc.getblockchaininfo()
        return rpc

    def render_url(self, password_mask=False):
        if password_mask == True:
            password = "xxxxxx"
        else:
            password = self.rpcpassword
        return "http://{}:{}@{}:{}/wallet/".format(
            self.rpcuser, password, self.ipaddress, self.rpcport
        )

    def as_data(self):
        """ returns a data-representation of this connection """
        me = {
            "user": self.rpcuser,
            "password": self.rpcpassword,
            "host": self.ipaddress,
            "port": self.rpcport,
            "url": self.render_url(),
        }
        return me

    def render_json(self):
        """ returns a json-representation of this connection """
        return json.dumps(self.as_data())

    def __repr__(self):
        return "<Btcd_conn {}>".format(self.render_url())


class NodeController:
    """ A kind of abstract class to simplify running a bitcoind with or without docker """

    def __init__(
        self,
        rpcport=18443,
        network="regtest",
        rpcuser="bitcoin",
        rpcpassword="secret",
        node_impl="bitcoin",
    ):
        try:
            self.rpcconn = Btcd_conn(
                rpcuser=rpcuser, rpcpassword=rpcpassword, rpcport=rpcport
            )
            self.network = network
            self.node_impl = node_impl
        except Exception as e:
            logger.exception(f"Failed to instantiate BitcoindController. Error: {e}")
            raise e

    def start_node(
        self,
        cleanup_at_exit=False,
        cleanup_hard=False,
        datadir=None,
        extra_args=[],
        timeout=60,
    ):
        """starts bitcoind with a specific rpcport=18543 by default.
        That's not the standard in order to make pytest running while
        developing locally against a different regtest-instance
        if bitcoind_path == docker, it'll run bitcoind via docker.
        Specify a longer timeout for slower devices (e.g. Raspberry Pi)
        """
        if self.check_existing() != None:
            return self.rpcconn

        logger.debug(f"Starting {self.node_impl}d")
        self._start_node(
            cleanup_at_exit,
            cleanup_hard=cleanup_hard,
            datadir=datadir,
            extra_args=extra_args,
        )

        try:
            self.wait_for_node(self.rpcconn, timeout=timeout)
            self.status = "Running"
        except ExtProcTimeoutException as e:
            if self.node_proc.poll() is None:
                self.status = "error"
                raise Exception(f"Could not start node due to: {self.node_proc.stdout}")
            self.status = "Starting up"
            logger.error(self.node_proc.stdout)
            raise e
        except Exception as e:
            self.status = "Error"
            raise e
        if self.network == "regtest":
            self.mine(block_count=100)

        return self.rpcconn

    def version(self):
        raise Exception("version needs to be overridden by Subclassses")

    def get_rpc(self):
        """ wrapper for convenience """
        return self.rpcconn.get_rpc()

    def _start_node(
        self, cleanup_at_exit, cleanup_hard=False, datadir=None, extra_args=[]
    ):
        raise Exception("This should not be used in the baseclass!")

    def check_existing(self):
        raise Exception("This should not be used in the baseclass!")

    def stop_node(self):
        raise Exception("This should not be used in the baseclass!")

    def mine(self, address="mruae2834buqxk77oaVpephnA5ZAxNNJ1r", block_count=1):
        """ Does mining to the attached address with as many as block_count blocks """
        self.rpcconn.get_rpc().generatetoaddress(block_count, address)

    def testcoin_faucet(self, address, amount=20, mine_tx=False):
        """ an easy way to get some testcoins """
        rpc = self.get_rpc()
        try:
            test3rdparty_rpc = rpc.wallet("test3rdparty")
            test3rdparty_rpc.getbalance()
        except RpcError as rpce:
            # return-codes:
            # https://github.com/bitcoin/bitcoin/blob/v0.15.0.1/src/rpc/protocol.h#L32L87
            if rpce.error_code == -18:  # RPC_WALLET_NOT_FOUND
                logger.debug("Creating test3rdparty wallet")
                rpc.createwallet("test3rdparty")
                test3rdparty_rpc = rpc.wallet("test3rdparty")
            else:
                raise rpce
        balance = test3rdparty_rpc.getbalance()
        if balance < amount:
            test3rdparty_address = test3rdparty_rpc.getnewaddress("test3rdparty")
            rpc.generatetoaddress(102, test3rdparty_address)
        test3rdparty_rpc.sendtoaddress(address, amount)
        if mine_tx:
            rpc.generatetoaddress(1, test3rdparty_address)

    @staticmethod
    def check_node(rpcconn, raise_exception=False):
        """ returns true if bitcoind is running on that address/port """
        if raise_exception:
            rpcconn.get_rpc()  # that call will also check the connection
            return True
        try:
            rpcconn.get_rpc()  # that call will also check the connection
            return True
        except RpcError as e:
            # E.g. "Loading Index ..." #ToDo: check it here
            return False
        except ConnectionError as e:
            return False
        except MaxRetryError as e:
            return False
        except TypeError as e:
            return False
        except NewConnectionError as e:
            return False
        except Exception as e:
            # We should avoid this:
            # If you see it in the logs, catch that new exception above
            logger.error("Unexpected Exception, THIS SHOULD NOT HAPPEN " + str(type(e)))
            logger.debug(f"could not reach bitcoind - message returned: {e}")
            return False

    @staticmethod
    def wait_for_node(rpcconn, timeout=15):
        """ tries to reach the bitcoind via rpc. Timeout after n seconds """
        i = 0
        while True:
            if NodeController.check_node(rpcconn):
                break
            time.sleep(0.5)
            i = i + 1
            if i % 10 == 0:
                logger.info(f"Timeout reached in {timeout - i/2} seconds")
            if i > (2 * timeout):
                try:
                    NodeController.check_node(rpcconn, raise_exception=True)
                except Exception as e:
                    raise ExtProcTimeoutException(
                        f"Timeout while trying to reach node {rpcconn.render_url(password_mask=True)} because {e}".format(
                            rpcconn
                        )
                    )

    @staticmethod
    def render_rpc_options(rpcconn):
        options = " -rpcport={} -rpcuser={} -rpcpassword={} ".format(
            rpcconn.rpcport, rpcconn.rpcuser, rpcconn.rpcpassword
        )
        return options

    @classmethod
    def construct_node_cmd(
        cls,
        rpcconn,
        run_docker=True,
        datadir=None,
        node_path="bitcoind",
        network="regtest",
        extra_args=[],
    ):
        """ returns a command to run your node (bitcoind/elementsd) """
        btcd_cmd = '"{}" '.format(node_path)
        if network != "mainnet":
            btcd_cmd += " -{} ".format(network)
        btcd_cmd += " -fallbackfee=0.0002 "
        btcd_cmd += " -port={} -rpcport={} -rpcbind=0.0.0.0 -rpcbind=0.0.0.0".format(
            rpcconn.rpcport - 1, rpcconn.rpcport
        )
        btcd_cmd += " -rpcuser={} -rpcpassword={} ".format(
            rpcconn.rpcuser, rpcconn.rpcpassword
        )
        btcd_cmd += " -rpcallowip=0.0.0.0/0 -rpcallowip=172.17.0.0/16 "
        if not run_docker:
            btcd_cmd += " -noprinttoconsole"
            if datadir == None:
                datadir = tempfile.mkdtemp(prefix="bitcoind_datadir")
            btcd_cmd += ' -datadir="{}" '.format(datadir)
        if extra_args:
            btcd_cmd += " {}".format(" ".join(extra_args))
        logger.debug("constructed bitcoind-command: %s", btcd_cmd)
        return btcd_cmd


class NodePlainController(NodeController):
    """ A class controlling the bitcoind-process directly on the machine """

    def __init__(
        self,
        node_path="bitcoind",
        rpcport=18443,
        network="regtest",
        rpcuser="bitcoin",
        rpcpassword="secret",
        node_impl="bitcoin",
    ):
        try:
            super().__init__(
                rpcport=rpcport,
                network=network,
                rpcuser=rpcuser,
                rpcpassword=rpcpassword,
                node_impl="bitcoin",
            )
            self.node_path = node_path
            self.rpcconn.ipaddress = "localhost"
            self.status = "Down"
        except Exception as e:
            logger.exception(f"Failed to instantiate NodePlainController. Error: {e}")
            raise e

    def _start_node(
        self, cleanup_at_exit=True, cleanup_hard=False, datadir=None, extra_args=[]
    ):
        if datadir == None:
            datadir = tempfile.mkdtemp(prefix="specter_btc_regtest_plain_datadir_")

        node_cmd = self.construct_node_cmd(
            self.rpcconn,
            run_docker=False,
            datadir=datadir,
            node_path=self.node_path,
            network=self.network,
            extra_args=extra_args,
        )
        logger.debug("About to execute: {}".format(node_cmd))
        # exec will prevent creating a child-process and will make node_proc.terminate() work as expected
        self.node_proc = subprocess.Popen(
            ("exec " if platform.system() != "Windows" else "") + node_cmd,
            shell=True,
        )
        time.sleep(0.2)  # sleep 200ms (catch stdout of stupid errors)
        if not self.node_proc.poll() is None:
            raise Exception(
                f"Could not start node due to: stderr: {self.node_proc.stderr} stdout: {self.node_proc.stdout} "
            )
        logger.debug(
            f"Running ${self.node_impl}d-process with pid {self.node_proc.pid}"
        )

        if cleanup_at_exit:
            logger.info("Register function cleanup_node for SIGINT and SIGTERM")
            # atexit.register(cleanup_bitcoind)
            # This is for CTRL-C --> SIGINT
            signal.signal(signal.SIGINT, self.cleanup_node)
            # This is for kill $pid --> SIGTERM
            signal.signal(signal.SIGTERM, self.cleanup_node)

    def cleanup_node(self, cleanup_hard=None, datadir=None):
        timeout = 50  # in secs
        if cleanup_hard:
            self.node_proc.kill()  # might be usefull for e.g. testing. We can't wait for so long
            logger.info(
                f"Killed ${self.node_impl}d with pid {self.node_proc.pid}, Removing {datadir}"
            )
            shutil.rmtree(datadir, ignore_errors=True)
        else:
            try:
                self.node_proc.terminate()  # might take a bit longer than kill but it'll preserve block-height
                logger.info(
                    f"Terminated ${self.node_impl}d with pid {self.node_proc.pid}, waiting for termination (timeout {timeout} secs)..."
                )
                # self.node_proc.wait() # doesn't have a timeout
                procs = psutil.Process().children()
                for p in procs:
                    p.terminate()
                _, alive = psutil.wait_procs(procs, timeout=timeout)
                for p in alive:
                    logger.info(
                        f"${self.node_impl} did not terminated in time, killing!"
                    )
                    p.kill()
            except ProcessLookupError:
                # Bitcoind probably never came up or crashed. Silently ignored
                pass

        if platform.system() == "Windows":
            subprocess.run("Taskkill /IM bitcoind.exe /F")

    def stop_node(self):
        self.cleanup_node()
        self.status = "Down"

    def check_existing(self):
        """other then in docker, we won't check on the "instance-level". This will return true if a
        node is running on the default port.
        """
        if not self.check_node(self.rpcconn):
            if self.status == "Running":
                self.status = "Down"
            return None
        else:
            self.status = "Running"
            return True


def fetch_wallet_addresses_for_mining(data_folder):
    """parses all the wallet-jsons in the folder (default ~/.specter/wallets/regtest)
    and returns an array with the addresses
    """
    wallets = load_jsons(data_folder + "/wallets/regtest")
    address_array = [value["address"] for key, value in wallets.items()]
    # remove duplicates
    address_array = list(dict.fromkeys(address_array))
    return address_array
