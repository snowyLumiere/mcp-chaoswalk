"""Mock Enterprise Order & Warehouse MCP Server.

Models in-memory orders and locks with deliberate tool errors, finite delays,
and invalid business transitions for service-specific tests.
"""

import asyncio
import json
import time
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("mock-order-warehouse")

# In-memory state store
_orders: Dict[str, Dict[str, Any]] = {}
_locks: Dict[str, str] = {}
_order_counter = 1000


@server.tool()
def create_order(sku: str, quantity: int = 1, customer_id: str = "CUST-001") -> str:
    """Create a new draft order."""
    global _order_counter
    _order_counter += 1
    order_id = f"ORD-{_order_counter}"
    _orders[order_id] = {
        "order_id": order_id,
        "sku": sku,
        "quantity": quantity,
        "customer_id": customer_id,
        "status": "draft",
        "created_at": time.time(),
    }
    return json.dumps(_orders[order_id])


@server.tool()
def acquire_order_lock(order_id: str, operator_id: str = "operator_alpha") -> str:
    """Acquire an exclusive lock for modifying the order."""
    if order_id not in _orders:
        return json.dumps({"error": f"Order {order_id} not found", "is_error": True})
    
    _locks[order_id] = operator_id
    return json.dumps({"order_id": order_id, "locked_by": operator_id, "status": "locked"})


@server.tool()
def update_shipping_address(order_id: str, address: str) -> str:
    """Update delivery address. Requires order lock."""
    if order_id not in _locks:
        return json.dumps({"error": f"Cannot update {order_id}: Lock required", "is_error": True})
    
    if order_id not in _orders:
        return json.dumps({"error": f"Order {order_id} does not exist", "is_error": True})
        
    _orders[order_id]["address"] = address
    return json.dumps({"order_id": order_id, "address": address, "updated": True})


@server.tool()
def cancel_order(order_id: str) -> str:
    """Cancel and delete an order from active storage.
    
    BUG: Does NOT clear or validate _locks[order_id]!
    """
    if order_id in _orders:
        del _orders[order_id]
        return json.dumps({"order_id": order_id, "status": "canceled"})
    return json.dumps({"error": f"Order {order_id} not found", "is_error": True})


@server.tool()
def release_order_lock(order_id: str) -> str:
    """Release the lock on an order.
    
    FATAL BUG: Directly references _orders[order_id] without checking if cancel_order deleted it!
    Triggers an unhandled KeyError crash when called after cancel_order!
    """
    if order_id not in _locks:
        return json.dumps({"error": f"No active lock on {order_id}", "is_error": True})
    
    # UNHANDLED EXCEPTION BUG (Dirty Write crash):
    order = _orders[order_id]  # Crashes with KeyError: 'ORD-xxxx' if canceled!
    del _locks[order_id]
    return json.dumps({"order_id": order_id, "status": "lock_released", "order_status": order["status"]})


@server.tool()
async def reserve_inventory(order_id: str, sku: str, warehouse_id: str = "WH-EAST-1") -> str:
    """Reserve warehouse inventory for an order.
    
    Certain requests on a locked order add a finite response delay.
    """
    if "TURBO-99" in sku or "WH-WEST-2" in warehouse_id:
        if order_id in _locks:
            await asyncio.sleep(8.0)
            return json.dumps({"status": "reserved_after_delay"})
            
    return json.dumps({"order_id": order_id, "sku": sku, "status": "reserved"})


@server.tool()
def refund_order(order_id: str, amount: float = 99.0) -> str:
    """Refund order payment.
    
    STATE CONTRADICTION BUG: Silently refunds draft orders without checking fulfillment/payment.
    """
    return json.dumps({"order_id": order_id, "status": "refunded", "amount": amount})


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_order_status(order_id: str) -> str:
    """Query current status of an order."""
    if order_id in _orders:
        return json.dumps(_orders[order_id])
    return json.dumps({"order_id": order_id, "status": "not_found"})


if __name__ == "__main__":
    server.run(transport="stdio")
