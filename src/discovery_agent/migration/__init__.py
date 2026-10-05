"""Moving discovered Synapse objects into Microsoft Fabric.

The only package that writes anywhere. Discovery reads; this creates Fabric
items (notebooks) and Warehouse objects (tables, views, procedures), and never
overwrites something that already exists.
"""
