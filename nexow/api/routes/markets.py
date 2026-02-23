"""IG Markets API — categories, instruments, markets."""

from fastapi import APIRouter, HTTPException, Query

from nexow.broker.ig import IGClient, IGForbiddenError

router = APIRouter(prefix="/api/markets", tags=["markets"])

_ig: IGClient | None = None


def get_ig() -> IGClient:
    global _ig
    if _ig is None:
        _ig = IGClient()
    return _ig


@router.get("/categories")
async def get_categories():
    """Returns a list of all categories of instruments enabled for the IG account."""
    try:
        ig = get_ig()
        categories = await ig.get_categories()
        return {"categories": categories}
    except IGForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/categories/{category_id}/instruments")
async def get_category_instruments(category_id: str):
    """Returns all instruments for the given category."""
    try:
        ig = get_ig()
        instruments = await ig.get_category_instruments(category_id)
        return {"instruments": instruments}
    except IGForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search")
async def search_markets(
    search_term: str = Query(..., min_length=1, alias="searchTerm"),
):
    """Returns all markets matching the search term."""
    try:
        ig = get_ig()
        markets = await ig.search_markets(search_term)
        return {"markets": markets}
    except IGForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/prices/{epic}")
async def get_prices(
    epic: str,
    resolution: str = Query(default="MINUTE"),
    num_points: int = Query(default=100, ge=1, le=500),
):
    """Returns historical prices for the given epic."""
    try:
        ig = get_ig()
        prices = await ig.get_prices(epic, resolution=resolution, num_points=num_points)
        return {"prices": prices}
    except IGForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{epic}")
async def get_market(epic: str):
    """Returns details of the given market by epic."""
    try:
        ig = get_ig()
        market = await ig.get_market(epic)
        if market is None:
            raise HTTPException(status_code=404, detail=f"Market '{epic}' not found")
        return market
    except HTTPException:
        raise
    except IGForbiddenError as e:
        raise HTTPException(status_code=403, detail=e.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
