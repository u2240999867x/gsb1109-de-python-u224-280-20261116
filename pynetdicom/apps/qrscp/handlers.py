"""Event handlers for qrscp.py"""

import os

from pydicom import dcmread
from pydicom.dataset import Dataset
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from pynetdicom.apps.qrscp.db import Instance, InvalidIdentifier, add_instance, search
from pynetdicom.sop_class import StorageCommitmentPushModel


def handle_echo(event, cli_config, logger):
    """Handler for evt.EVT_C_ECHO.

    Parameters
    ----------
    event : events.Event
        The corresponding event.
    cli_config : dict
        A :class:`dict` containing configuration settings passed via CLI.
    logger : logging.Logger
        The application's logger.

    Returns
    -------
    int
        The status of the C-ECHO operation, always ``0x0000`` (Success).
    """
    requestor = event.assoc.requestor
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    addr, port = requestor.address, requestor.port
    logger.info(f"Received C-ECHO request from {addr}:{port} at {timestamp}")

    return 0x0000


def handle_find(event, db_path, cli_config, logger):
    """Handler for evt.EVT_C_FIND.

    Parameters
    ----------
    event : pynetdicom.events.Event
        The C-FIND request :class:`~pynetdicom.events.Event`.
    db_path : str
        The database path to use with create_engine().
    cli_config : dict
        A :class:`dict` containing configuration settings passed via CLI.
    logger : logging.Logger
        The application's logger.

    Yields
    ------
    int or pydicom.dataset.Dataset, pydicom.dataset.Dataset or None
        The C-FIND response's *Status* and if the *Status* is pending then
        the dataset to be sent, otherwise ``None``.
    """
    requestor = event.assoc.requestor
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    addr, port = requestor.address, requestor.port
    logger.info(f"Received C-FIND request from {addr}:{port} at {timestamp}")

    model = event.request.AffectedSOPClassUID

    if model.keyword in (
        "UnifiedProcedureStepPull",
        "ModalityWorklistInformationModelFind",
    ):
        yield 0x0000, None
    else:
        engine = create_engine(db_path)
        with engine.connect() as conn:  # noqa: F841
            Session = sessionmaker(bind=engine)
            session = Session()
            # Search database using Identifier as the query
            try:
                matches = search(model, event.identifier, session)

            except InvalidIdentifier as exc:
                session.rollback()
                logger.error("Invalid C-FIND Identifier received")
                logger.error(str(exc))
                yield 0xA900, None
                return
            except Exception:
                session.rollback()
                logger.exception("Exception occurred while querying database")
                yield 0xC320, None
                return
            finally:
                session.close()

        # Yield results
        for match in matches:
            if event.is_cancelled:
                yield 0xFE00, None
                return

            try:
                response = match.as_identifier(event.identifier, model)
                response.RetrieveAETitle = event.assoc.ae.ae_title
            except Exception:
                logger.exception("Error creating response Identifier")
                yield 0xC322, None

            yield 0xFF00, response


def handle_get(event, db_path, cli_config, logger):
    """Handler for evt.EVT_C_GET.

    Parameters
    ----------
    event : pynetdicom.events.Event
        The C-GET request :class:`~pynetdicom.events.Event`.
    db_path : str
        The database path to use with create_engine().
    cli_config : dict
        A :class:`dict` containing configuration settings passed via CLI.
    logger : logging.Logger
        The application's logger.

    Yields
    ------
    int
        The number of sub-operations required to complete the request.
    int or pydicom.dataset.Dataset, pydicom.dataset.Dataset or None
        The C-GET response's *Status* and if the *Status* is pending then
        the dataset to be sent, otherwise ``None``.
    """
    requestor = event.assoc.requestor
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    addr, port = requestor.address, requestor.port
    logger.info(f"Received C-GET request from {addr}:{port} at {timestamp}")

    model = event.request.AffectedSOPClassUID

    engine = create_engine(db_path)
    with engine.connect() as conn:  # noqa: F841
        Session = sessionmaker(bind=engine)
        session = Session()
        # Search database using Identifier as the query
        try:
            matches = search(model, event.identifier, session)
        except InvalidIdentifier as exc:
            session.rollback()
            logger.error("Invalid C-GET Identifier received")
            logger.error(str(exc))
            yield 0xA900, None
            return
        except Exception:
            session.rollback()
            logger.exception("Exception occurred while querying database")
            yield 0xC420, None
            return
        finally:
            session.close()

    # Yield number of sub-operations
    yield len(matches)

    # Yield results
    for match in matches:
        if event.is_cancelled:
            yield 0xFE00, None
            return

        try:
            ds = dcmread(match.filename)
        except Exception:
            logger.exception(f"Error reading file: {match.filename}")
            yield 0xC421, None

        yield 0xFF00, ds


def handle_move(event, destinations, db_path, cli_config, logger):
    """Handler for evt.EVT_C_MOVE.

    Parameters
    ----------
    event : pynetdicom.events.Event
        The C-MOVE request :class:`~pynetdicom.events.Event`.
    destinations : dict
        A :class:`dict` containing know move destinations as
        ``{b'AE_TITLE: (addr, port)}``
    db_path : str
        The database path to use with create_engine().
    cli_config : dict
        A :class:`dict` containing configuration settings passed via CLI.
    logger : logging.Logger
        The application's logger.

    Yields
    ------
    (str, int) or (None, None)
        The (IP address, port) of the *Move Destination* (if known).
    int
        The number of sub-operations required to complete the request.
    int or pydicom.dataset.Dataset, pydicom.dataset.Dataset or None
        The C-MOVE response's *Status* and if the *Status* is pending then
        the dataset to be sent, otherwise ``None``.
    """
    requestor = event.assoc.requestor
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    addr, port = requestor.address, requestor.port
    logger.info(
        f"Received C-MOVE request from {addr}:{port} at {timestamp} "
        f"with move destination {event.move_destination}"
    )

    # Unknown `Move Destination`
    try:
        addr, port = destinations[event.move_destination]
    except KeyError:
        logger.info("No matching move destination in the configuration")
        yield None, None
        return

    model = event.request.AffectedSOPClassUID
    engine = create_engine(db_path)
    with engine.connect() as conn:  # noqa: F841
        Session = sessionmaker(bind=engine)
        session = Session()
        # Search database using Identifier as the query
        try:
            matches = search(model, event.identifier, session)
        except InvalidIdentifier as exc:
            session.rollback()
            logger.error("Invalid C-MOVE Identifier received")
            logger.error(str(exc))
            yield 0xA900, None
            return
        except Exception:
            session.rollback()
            logger.exception("Exception occurred while querying database")
            yield 0xC520, None
            return
        finally:
            session.close()

    # Yield `Move Destination` IP and port, plus required contexts
    # We should be able to reduce the number of contexts by using the
    # implicit context conversion between:
    #   implicit VR <-> explicit VR <-> deflated transfer syntaxes
    contexts = list({ii.context for ii in matches})
    yield addr, port, {"contexts": contexts[:128]}

    # Yield number of sub-operations
    yield len(matches)

    # Yield results
    for match in matches:
        if event.is_cancelled:
            yield 0xFE00, None
            return

        try:
            ds = dcmread(match.filename)
        except Exception:
            logger.exception(f"Error reading file: {match.filename}")
            yield 0xC521, None

        yield 0xFF00, ds


def handle_store(event, storage_dir, db_path, cli_config, logger, handover=None):
    """Handler for evt.EVT_C_STORE.

    Parameters
    ----------
    event : pynetdicom.events.Event
        The C-STORE request :class:`~pynetdicom.events.Event`.
    storage_dir : str
        The path to the directory where instances will be stored.
    db_path : str
        The database path to use with create_engine().
    cli_config : dict
        A :class:`dict` containing configuration settings passed via CLI.
    logger : logging.Logger
        The application's logger.
    handover : pynetdicom.apps.qrscp.handover.HandoverManager, optional
        If supplied, the successfully stored instance is added to the current
        transfer batch for later Storage Commitment correlation.

    Returns
    -------
    int or pydicom.dataset.Dataset
        The C-STORE response's *Status*. If the storage operation is successful
        but the dataset couldn't be added to the database then the *Status*
        will still be ``0x0000`` (Success).
    """
    requestor = event.assoc.requestor
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    addr, port = requestor.address, requestor.port
    logger.info(f"Received C-STORE request from {addr}:{port} at {timestamp}")

    try:
        ds = event.dataset
        # Remove any Group 0x0002 elements that may have been included
        ds = ds[0x00030000:]
        sop_instance = ds.SOPInstanceUID
    except Exception:
        logger.exception("Unable to decode the dataset")
        # Unable to decode dataset
        return 0xC210

    # Add the file meta information elements - must be before adding to DB
    ds.file_meta = event.file_meta

    logger.info(f"SOP Instance UID '{sop_instance}'")

    # Try and add the instance to the database
    #   If we fail then don't even try to store
    fpath = os.path.join(storage_dir, sop_instance)

    if os.path.exists(fpath):
        logger.warning("Instance already exists in storage directory, overwriting")

    try:
        ds.save_as(fpath, write_like_original=False)
    except Exception:
        logger.exception("Failed writing instance to storage directory")
        # Failed - Out of Resources
        return 0xA700

    logger.info("Instance written to storage directory")

    # Dataset successfully written, try to add to/update database
    engine = create_engine(db_path)
    with engine.connect() as conn:  # noqa: F841
        Session = sessionmaker(bind=engine)
        session = Session()

        try:
            # Path is relative to the database file
            matches = (
                session.query(Instance)
                .filter(Instance.sop_instance_uid == ds.SOPInstanceUID)
                .all()
            )
            add_instance(ds, session, os.path.abspath(fpath))
            if not matches:
                logger.info("Instance added to database")
            else:
                logger.info("Database entry for instance updated")
        except Exception:
            session.rollback()
            logger.exception("Unable to add instance to the database")
        finally:
            session.close()

    if handover is not None:
        handover.note_store(event.assoc, ds, event.timestamp)

    return 0x0000


def handle_n_action(event, handover, logger):
    """Handler for evt.EVT_N_ACTION.

    A Storage Commitment N-ACTION request defines a transaction and its
    instance manifest.  The request is recorded as the completed transfer for
    the batch when the same association has just delivered C-STORE requests.

    Returns
    -------
    int, pydicom.dataset.Dataset or None
        The N-ACTION response *Status* and optional *Action Reply*.
    """
    requestor = event.assoc.requestor
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    addr, port = requestor.address, requestor.port
    logger.info(f"Received N-ACTION request from {addr}:{port} at {timestamp}")

    if event.request.RequestedSOPClassUID != StorageCommitmentPushModel:
        logger.warning("N-ACTION request is not for Storage Commitment")
        return 0x0123, None

    try:
        record = handover.record_request(event)
    except Exception:
        logger.exception("Unable to record the Storage Commitment request")
        return 0x0110, None

    if record is None:
        logger.warning("Storage Commitment request was not linked to a transfer")
    else:
        logger.info(
            f"Storage Commitment request for transaction "
            f"'{record.transaction_uid}' recorded"
        )

    reply = Dataset()
    reply.TransactionUID = handover.request_transaction_uid(event)
    return 0x0000, reply


def handle_n_event_report(event, handover, logger):
    """Handler for evt.EVT_N_EVENT_REPORT.

    The commitment result is matched to its transfer by transaction UID only.
    A result for an unknown transaction is retained by the archive as an
    unmatched report.

    Returns
    -------
    int, None
        The N-EVENT-REPORT response *Status*.
    """
    requestor = event.assoc.requestor
    timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    addr, port = requestor.address, requestor.port
    logger.info(
        f"Received N-EVENT-REPORT request from {addr}:{port} at {timestamp}"
    )

    if event.request.AffectedSOPClassUID != StorageCommitmentPushModel:
        logger.warning("N-EVENT-REPORT request is not for Storage Commitment")
        return 0x0113, None

    try:
        record = handover.record_report(event)
    except Exception:
        logger.exception("Unable to record the Storage Commitment result")
        return 0x0110, None

    if record is None:
        logger.warning(
            "Storage Commitment result is for an unknown transaction and "
            "was retained separately"
        )
    else:
        logger.info(
            f"Storage Commitment result for transaction "
            f"'{record.transaction_uid}' updated handover status to "
            f"'{record.status.value}'"
        )

    return 0x0000, None


def handle_release(event, handover):
    """Handler for evt.EVT_RELEASED and evt.EVT_ABORTED.

    Clears the temporary C-STORE batch associated with the closed
    association.
    """
    handover.release(event.assoc)
