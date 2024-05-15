from flask import Response, request, url_for

from lxml import etree
from MyCapytain.common.constants import RDF_NAMESPACES, \
    Mimetypes, \
    XPATH_NAMESPACES
from MyCapytain.common.reference import BaseCitationSet, BaseReference, BaseReferenceSet, URN, CtsReference
from MyCapytain.common.utils import Subgraph, literal_to_dict
from MyCapytain.resources.prototypes.metadata import Collection
from typing import Callable, Tuple
from rdflib import URIRef, RDF, RDFS, Graph
from rdflib.namespace import DCTERMS, NamespaceManager, DC
from collections import OrderedDict
import json
from link_header import LinkHeader, Link

from capitains_nautilus.apis.base import AdditionalAPIPrototype, \
    query_parameters_as_kwargs
from capitains_nautilus.errors import NautilusError, CtsUnknownCollection, CtsMissingParameter


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
    if direction == "parents":
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

    .. todo:: Evaluate how much hardcoding prefix terms improve perfs
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
            isinstance(collection.citation, BaseCitationSet):
        if collection.readable:
            j["dts:citeDepth"] = collection.citation.depth
        if not collection.citation.is_empty():
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


def _single_ref_or_diff_reff(start: BaseReference, end: BaseReference):
    start = str(start.start)
    if end.is_range():
        end = str(end.end)
    else:
        end = str(end.start)

    if start == end:
        return start, None
    else:
        return start, end


def _cts_reference_grouper(cls, group_by: int, level: int, reffs: BaseReferenceSet):
    """ Alternative to level_chunker: groups levels together at the latest level

    Probably need to build something that could avoid this issue of "."
    """
    _refs = OrderedDict()

    for single_reff in reffs:
        for part_of_reff in single_reff:
            if part_of_reff:
                k = ".".join(part_of_reff.list[:level-1])
                if k not in _refs:
                    _refs[k] = []
                _refs[k].append(single_reff)
                del k

    return [
        cls(_single_ref_or_diff_reff(ref[0], ref[-1]))
        for sublist in _refs.values()
        for ref in [
            sublist[i:i+group_by]
            for i in range(0, len(sublist), group_by)
        ]
    ]


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


def _ref_to_dict(ref: BaseReference) -> dict:
    if ref.is_range():
        return {"start": ref.start, "end": ref.end}
    return {"ref": ref.start}


def _define_passage_id_from_params(objectId: str, passageId: str, start: str, end: str) -> Tuple[BaseReference, str]:
    refClass = BaseReference
    if objectId.startswith("urn:cts:"):
        urn = URN(objectId)
        objectId = urn.upTo(urn.NO_PASSAGE)
        if urn.reference and urn.reference.start is not None:
            passageId = urn.reference, str(objectId)
        else:
            refClass = CtsReference

    if not isinstance(passageId, BaseReference):
        if start and end:
            passageId = refClass(start, end)
        elif passageId:
            passageId = refClass(passageId)
    return passageId, objectId


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
        "r_dts_main"
    ]

    def __init__(self, expand_readable: bool=True, _external: bool=False):
        super(DTSApi, self).__init__()
        self._external = _external
        self._expand_readable = expand_readable

    def dts_error(self, error_name, message=None, debug="", http_code=404):
        """ Create a DTS Error reply

        :param error_name: Name of the error
        :param message: Message of the Error
        :param debug: Debug message sent to logger
        :return: DTS Error Response with information (JSON)
        """
        self.nautilus_extension.logger.info("DTS error thrown {} for {} ({}) (Debug : {})".format(
            error_name, request.path, message, debug
        ))

        code = http_code
        j = jsonify(
            {
                "@context": "http://www.w3.org/ns/hydra/context.jsonld",
                "@type": "Status",
                "statusCode": code,
                "title": error_name,
                "description": message.replace(
                    "GetValidReff", "Navigation Endpoint"
                ).replace(
                    "GetPassagePlus", "Document Endpoint"
                ).replace(
                    "GetPassage", "Document Endpoint"
                )
            }
        )
        j.status_code = code
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

        except CtsUnknownCollection as E:
            return self.dts_error(
                error_name="UnknownCollection",
                message=E.__doc__,
                debug="Resource {} not found".format(objectId)
            )
        except NautilusError as E:
            return self.dts_error(
                error_name=E.title,
                message=E.description,
            )
        j = jsonify(j)
        j.status_code = 200
        return j

    @query_parameters_as_kwargs(
        mapping={"id": "objectId", "ref": "passageId", "groupBy": "group_by"},
        params={
            "id": None,
            "ref": None,
            "start": None,
            "end": None,
            "level": 1,
            "groupBy": 1
        },
        typing={
            "level": int,
            "groupBy": int
        }
    )
    def r_dts_navigation(self, objectId=None, passageId=None, start=None, end=None, level=1, group_by=1):
        if not objectId:
            raise Exception()

        passageId, objectId = _define_passage_id_from_params(objectId, passageId, start, end)

        params = {
            k: v
            for k, v in {
                "id": objectId,
                "ref": passageId,
                "start": start,
                "end": end,
                "level": level,
                "groupBy": group_by
            }.items()
            if v
        }
        if start and end:
            del params["ref"]

        try:
            references = self.resolver.getReffs(
                textId=objectId,
                subreference=passageId,
                level=level
            )
        except NautilusError as E:
            return self.dts_error(
                error_name=E.title,
                message=E.description,
            )

        group_by = int(group_by)
        if group_by > 1:
            try:
                references = type(references)(
                    _cts_reference_grouper(
                        cls=type(references[0]),
                        group_by=group_by,
                        reffs=references,
                        level=references.level
                    ),
                    citation=references.citation,
                    level=references.level
                )
            except Exception:
                return self.dts_error("GroupingReffsError",
                                      "Grouping of reference failed", http_code=500)

        response = {
            "@context": {
                "hydra": "https://www.w3.org/ns/hydra/core#",
                "@vocab": "https://w3id.org/dts/api#"
            },
            "level": references.level,
            "passage": url_for(".dts_document", id=objectId, _external=self._external)+"{&ref}{&start}{&end}",
            "@id": objectId,
            "dts:references": url_for(".dts_navigation", **params),
            "hydra:member": [_ref_to_dict(ref) for ref in references]
        }
        if references.citation:
            response["citeDepth"] = references.citation.root.depth
            if references.citation.name:
                response["citeType"] = references.citation.name

        return jsonify(response)

    @query_parameters_as_kwargs(
        mapping={"id": "objectId", "ref": "passageId"},
        params={
            "id": None,
            "ref": None,
            "start": None,
            "end": None
        }
    )
    def r_dts_document(self, objectId=None, passageId=None, start=None, end=None):
        if not objectId:
            raise CtsMissingParameter("A document 'id' is required")

        passageId, objectId = _define_passage_id_from_params(objectId, passageId, start, end)

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

        # toDo : add navigation
        links = {
            "collection": url_for(".dts_collection", id=objectId)
        }
        if passageId:
            _prev, _next = self.resolver.getSiblings(objectId, passageId)
            links["contents"] = url_for(".dts_navigation", id=objectId, **_ref_to_dict(passageId))
            if _prev:
                links["prev"] = url_for(".dts_document", id=objectId, **_ref_to_dict(_prev))
            if _next:
                links["next"] = url_for(".dts_document", id=objectId, **_ref_to_dict(_next))
            if isinstance(passageId, CtsReference):
                if passageId.parent:
                    links["up"] = url_for(".dts_document", id=objectId, **_ref_to_dict(passageId.parent))

        else:
            links["contents"] = url_for(".dts_navigation", id=objectId)

        link_header = LinkHeader([Link(link, rel=rel) for rel, link in links.items()])

        return Response(
            outputXML,
            headers={
                "Content-Type": "application/xml",
                "Link": str(link_header)
            }
        )
