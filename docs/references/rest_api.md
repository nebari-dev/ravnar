---
hide:
  - toc
---

# REST API reference

<div id="redoc-container"></div>
<script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"> </script>
<script>
  Redoc.init(
    '../openapi.json',
    {
      theme: {
        breakpoints: {
          // Set the breakpoint for the 3-panel layout to a value that will never be reached.
          medium: '99999px'
        }
      }
    },
    document.getElementById('redoc-container')
  )
</script>
