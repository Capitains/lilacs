from flask import Response, request, url_for

from lxml import etree
from MyCapytain.common.constants import RDF_NAMESPACES, \
    Mimetypes, \
    XPATH_NAMESPACES
from MyCapytain.common.reference import BaseCitationSet
from MyCapytain.common.utils import Subgraph, literal_to_dict
from MyCapytain.resources.prototypes.metadata import Collection
from typing import Callable
from rdflib import URIRef, RDF, RDFS, Graph
from rdflib.namespace import DCTERMS, NamespaceManager, DC

import json

from capitains_nautilus.apis.base import AdditionalAPIPrototype, \
    query_parameters_as_kwargs
from capitains_nautilus.errors import NautilusError, UnknownCollection


_ns_hydra_str = str(RDF_NAMESPACES.HYDRA)
_ns_cts_str = str(RDF_NAMESPACES.CTS)
_ns_dts_str = str(RDF_NAMESPACES.DTS)
_ns_dct_str = str(DCTERMS)
_ns_cap_str = str(RDF_NAMESPACES.CAPITAINS)
_ns_rdf_str = str(RDF)
_ns_rdfs_str = str(RDFS)
_ignore_ns_for_bindings = [_ns_cap_str, _ns_hydra_str, _ns_rdf_str, _ns_rdfs_str]

def jsonify(response):
    return Response(json.dumps(response), headers={
        "Content-Type": "application/ld+json"
    })


def _collection_type(collection: Collection) -> str:
    if collection.readable:
        return "Resource"
    return "Collection"


def _nav_direction(collection, direction):
    if direction == "children":
        return collection.members
    if direction == "parent":
        return [collection.parent]


def _compute_extension_and_dc(o: dict, store: Subgraph,  nsm: NamespaceManager):
    """ Updates `o` with its dts:dublincore and dts:extensions metadata

    :param o: Dictionary of DTS computed response
    :param graph: Graph where the data lies
    :param nsm: Namespace manager for prefix building
    """
    # Builds the specific Store data
    extensions = {}
    dublincore = {}
    ignore_ns = [_ns_cap_str, _ns_hydra_str, _ns_rdf_str, _ns_rdfs_str, _ns_dts_str]

    # Builds the .dublincore and .extensions graphs
    for _, predicate, obj in store.graph:
        k = store.graph.qname(predicate)
        prefix, namespace, name = nsm.compute_qname(predicate)
        namespace = str(namespace)

        # Ignore namespaces that are part of the root DTS object
        if namespace in ignore_ns:
            continue

        # Switch to the correct container depending on namespaces
        if namespace == _ns_dct_str:
            metadata = dublincore
        else:
            metadata = extensions

        if k in metadata:
            if isinstance(metadata[k], list):
                metadata[k].append(literal_to_dict(obj))
            else:
                metadata[k] = [metadata[k], literal_to_dict(obj)]
        else:
            metadata[k] = literal_to_dict(obj)
            if isinstance(metadata[k], dict):
                metadata[k] = [metadata[k]]

    if extensions:
        o[nsm.qname(RDF_NAMESPACES.DTS.extensions)] = extensions

    if dublincore:
        o[nsm.qname(RDF_NAMESPACES.DTS.dublincore)] = dublincore


def _hydra_dts_predicates(graph: Graph, collection: Collection, nsm: NamespaceManager, _external: bool=False) -> dict:
    """ Computes the hydra and dts base predicates (routes, title, description, id)

    :param graph: Graph where the data lies
    :param collection: Collection for which we should build this item
    :param nsm: Namespace Manager
    :param _external: Optionnaly builds external for url_for
    :return: Base dictionary representation of the item

    .. todo:: Evalute how much hardcoding prefix terms such as title and so on
    """
    j = {
        "@id": collection.id,
        "@type": _collection_type(collection),
        "title": str(collection.get_label()),
        "totalItems": collection.size
    }

    for desc in graph.objects(collection.asNode(), RDF_NAMESPACES.HYDRA.description):
        j["description"] = str(desc)
    else:
        for desc in graph.objects(collection.asNode(), DCTERMS.description):
            j["description"] = str(desc)
        else:
            for desc in graph.objects(collection.asNode(), DC.description):
                j["description"] = str(desc)

    if collection.readable:
        j["dts:passage"] = url_for(".dts_document", id=collection.id, _external=_external)
        j["dts:references"] = url_for(".dts_navigation", id=collection.id, _external=_external)

    for download_uri in graph.objects(collection.asNode(), RDF_NAMESPACES.DTS.term("download")):
        j["dts:download"] = download_uri

    # If the system handles citation structure
    if hasattr(collection, "citation") and \
            isinstance(collection.citation, BaseCitationSet) and \
            not collection.citation.is_empty():
        j["dts:citeStructure"] = collection.citation.export(
            Mimetypes.JSON.DTS.Std,
            context=False,
            namespace_manager=nsm
        )

    return j


def _build_subgraph(collection: Collection, nsm: dict) -> (Subgraph, Graph, dict, NamespaceManager):
    """ Builds the subgraph for a given collection

    :param collection: Collection for which to retrieve data
    :param nsm: Dictionary of prefix -> namespaces
    :return: Restricted subgraph, Graph of the subgraph, @context, NamespaceManager
    """
    # Set-up a derived graph
    store = Subgraph(nsm)
    store.graphiter(collection.graph, collection.asNode(), ascendants=0, descendants=1)
    graph = store.graph
    nsm = store.graph.namespace_manager

    # Build the JSON-LD @context
    bindings = {}
    for predicate in set(graph.predicates()):
        prefix, namespace, name = nsm.compute_qname(predicate)
        if prefix not in bindings and str(namespace) not in _ignore_ns_for_bindings:
            bindings[prefix] = str(URIRef(namespace))

    return store, graph, bindings, nsm


def _export_subcollection(
        collection: Collection,
        namespace_manager: NamespaceManager,
        original_bindings: dict,
        expand: Callable=None,
        _external: bool=False) -> dict:

    nsm = dict(namespace_manager.namespaces())
    store, graph, bindings, nsm = _build_subgraph(collection, nsm)

    # Base DTS / Hydra Export
    o = _hydra_dts_predicates(graph, collection, nsm, _external=_external)

    if expand and expand(collection):  # Add dts:dublincore and dts:extensions
        _compute_extension_and_dc(o, store, nsm)
        original_bindings.update(bindings)

    return o


def _export_collection_dts(
        collection: Collection, members: list,
        namespace_manager: NamespaceManager= None,
        expand_members: Callable=None,
        _external: bool=False) -> dict:
    """ Builds the JSON-LD response for DTS Collection

    :param collection:
    :param namespace_manager:
    :param expand_members:
    :param _external:
    :return:
    """
    # Set-up a derived Namespace Manager
    if not namespace_manager:
        nsm = {
            "": RDF_NAMESPACES.HYDRA,
            "cts": RDF_NAMESPACES.CTS,
            "dts": RDF_NAMESPACES.DTS,
            "dct": DCTERMS
        }
    else:
        nsm = dict(namespace_manager.namespaces())

    store, graph, bindings, nsm = _build_subgraph(collection, nsm)

    # Base DTS / Hydra Export
    o = _hydra_dts_predicates(graph, collection, nsm, _external=_external)

    # Add dts:dublincore and dts:extensions
    _compute_extension_and_dc(o, store, nsm)

    if collection.size:
        o[nsm.qname(RDF_NAMESPACES.HYDRA.member)] = [
            _export_subcollection(member, nsm, bindings, expand=expand_members, _external=_external)
            for member in members
        ]

    # Set up bindings
    o["@context"] = bindings
    o["@context"]["@vocab"] = _ns_hydra_str

    del store
    return o


class DTSApi(AdditionalAPIPrototype):
    NAME = "DTS"
    ROUTES = [
        ('/dts', "r_dts_main", ["GET", "OPTIONS"]),
        ('/dts/collections', "r_dts_collection", ["GET", "OPTIONS"]),
        ('/dts/document', "r_dts_document", ["GET", "OPTIONS"]),
        ('/dts/navigation', "r_dts_navigation", ["GET", "OPTIONS"])
    ]
    Access_Control_Allow_Methods = {
        "r_dts_collection": "OPTIONS, GET",
        "r_dts_main": "OPTIONS, GET",
        "r_dts_document": "OPTIONS, GET",
        "r_dts_navigation": "OPTIONS, GET"
    }
    CACHED = [
        #  DTS
        "r_dts_collection",
        "dts_error",
        "r_dts_main"
    ]

    def __init__(self, expand_readable: bool=True, _external: bool=False):
        super(DTSApi, self).__init__()
        self._external = _external
        self._expand_readable = expand_readable

    def dts_error(self, error_name, message=None, debug=""):
        """ Create a DTS Error reply

        :param error_name: Name of the error
        :param message: Message of the Error
        :param debug: Debug message sent to logger
        :return: DTS Error Response with information (JSON)
        """
        self.nautilus_extension.logger.info("DTS error thrown {} for {} ({}) (Debug : {})".format(
            error_name, request.path, message, debug
        ))
        j = jsonify({
                "error": error_name,
                "message": message
            })
        j.status_code = 404
        return j

    def r_dts_main(self):
        return jsonify({
          "@context": "dts/EntryPoint.jsonld",
          "@id": url_for(".dts_main", _external=self._external),
          "@type": "EntryPoint",

          "collections": url_for(".dts_collection", _external=self._external),
          "documents": url_for(".dts_document", _external=self._external),
          "navigation": url_for(".dts_navigation", _external=self._external)
        })

    @query_parameters_as_kwargs(
        mapping={"id": "objectId", "nav": "direction"},
        params={
            "id": None,
            "nav": "children"
        }
    )
    def r_dts_collection(self, objectId, direction):
        """ DTS Collection Metadata reply for given objectId

        :return: JSON Format of DTS Collection
        """

        try:
            collection = self.resolver.getMetadata(objectId=objectId)
            j = _export_collection_dts(
                collection,
                _nav_direction(collection, direction),
                expand_members=lambda obj: self._expand_readable and obj.readable
            )

        except UnknownCollection as E:
            return self.dts_error(
                error_name="UnknownCollection",
                message=E.__doc__,
                debug="Resource {} not found".format(objectId)
            )
        except NautilusError as E:
            return self.dts_error(
                error_name=E.__class__.__name__,
                message=E.__doc__
            )
        j = jsonify(j)
        j.status_code = 200
        return j

    @query_parameters_as_kwargs(
        mapping={"id": "objectId", "passage": "passageId"},
        params={
            "id": None,
            "passage": None,
            "start": None,
            "end": None,
            "level": 1
        }
    )
    def r_dts_navigation(self, objectId=None, passageId=None, start=None, end=None, level=1):
        if not objectId:
            raise Exception()
        if start and end:
            # Currently hacked to work only with CTS Identifier
            # See https://github.com/Capitains/MyCapytain/issues/161
            references = self.resolver.getReffs(
                textId=objectId,
                subreference="-".join([start, end]),
                level=level
            )
        else:
            references = self.resolver.getReffs(
                textId=objectId,
                subreference=passageId,
                level=level
            )
        return jsonify({
            "@context": {
                "passage": "https://w3id.org/dts/api#passage"
            },
            "@base": url_for(".dts_document", _external=self._external),
            "@id": objectId,
            "passage": references
        })

    @query_parameters_as_kwargs(
        mapping={"id": "objectId", "passage": "passageId"},
        params={
            "id": None,
            "passage": None,
            "start": None,
            "end": None
        }
    )
    def r_dts_document(self, objectId=None, passageId=None, start=None, end=None):

        if not objectId:
            raise Exception()
        if start and end:
            # Currently hacked to work only with CTS Identifier
            # See https://github.com/Capitains/MyCapytain/issues/161
            passage = self.resolver.getTextualNode(
                textId=objectId,
                subreference="-".join([start, end])
            )
        else:
            passage = self.resolver.getTextualNode(
                textId=objectId,
                subreference=passageId
            )

        if passageId:
            inputXML = passage.export(Mimetypes.PYTHON.ETREE)
            wrapper = etree.fromstring("<dts:fragment xmlns:dts='https://w3id.org/dts/api#' />")
            for container in inputXML.xpath("//tei:text", namespaces=XPATH_NAMESPACES):
                container.getparent().append(wrapper)
                wrapper.insert(0, container)
                break
            outputXML = etree.tostring(inputXML, encoding=str)
        else:
            outputXML = passage.export(Mimetypes.XML.TEI)

        return Response(
            outputXML,
            headers={
                "Content-Type": "application/tei+xml"
            }
        )
