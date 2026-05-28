"""
Cortex Connector Collection — Phase 1

All connectors live here and implement BaseConnector.
Auto-discovery: ConnectorManager imports from this package.
"""

from ingestion.connectors.base import BaseConnector, ConnectResult, Resource, RawDocument

__all__ = ["BaseConnector", "ConnectResult", "Resource", "RawDocument"]
