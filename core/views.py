"""The core app is a library of shared logic, not a set of endpoints.

Reusable behaviour lives in:
  - ``core.geo``          distance and bounding-box maths
  - ``core.compat``       blood-group compatibility
  - ``core.eligibility``  donation safety rules
  - ``core.models``       abstract bases (TimeStampedModel, GeoLocated)
  - ``core.forms``        form mixins including the map location picker
  - ``core.decorators``   role-based access control
"""
