import json
import logging
import inspect
from pprint import pprint
from typing import Optional, Any, List, Tuple, Union, Callable, Dict, Type

from instrumentserver.gui.misc import AlertLabelGreen
from qcodes import Parameter, Instrument

from . import parameters, keepSmallHorizontally
from .base_instrument import InstrumentDisplayBase, ItemBase, InstrumentModelBase, InstrumentTreeViewBase
from .parameters import ParameterWidget, AnyInput
from .. import QtWidgets, QtCore, QtGui
from ..blueprints import ParameterBroadcastBluePrint
from ..client import ProxyInstrument, SubClient
from ..helpers import stringToArgsAndKwargs, nestedAttributeFromString
from ..params import ParameterManager, paramTypeFromName, ParameterTypes, parameterTypes
from ..serialize import toParamDict
from ast import literal_eval

# TODO: all styles set through a global style sheet.
# TODO: [maybe] add a column for information on valid input values?

logger = logging.getLogger(__name__)


class ParameterManagerGui(QtWidgets.QWidget):
    #: Signal(str) --
    #: emitted when there's an error during parameter creation.
    parameterCreationError = QtCore.Signal(str)

    #: Signal() --
    #:  emitted when a parameter was created successfully
    parameterCreated = QtCore.Signal()

    def __init__(self, ins: Union[ParameterManager, ProxyInstrument],
                 parent: Optional[QtWidgets.QWidget] = None,
                 makeAvailable: Optional[List[Tuple[str, Any]]] = []):

        super().__init__(parent)

        for (modName, mod) in makeAvailable:
            setattr(parameters, modName, mod)

        self.setMinimumWidth(500)
        self.setMinimumHeight(300)

        self._instrument = ins
        self._widgets = {}
        self._removeWidgets = {}

        # make the main layout and set up all the widgets
        layout = QtWidgets.QVBoxLayout(self)

        # toolbar: refreshing, filtering, and some control over the tree
        self.toolbar = self._makeToolbar()
        layout.addWidget(self.toolbar)

        # the main widget for displaying the parameters
        self.plist = ParameterList(self)
        layout.addWidget(self.plist)

        # at the bottom, a widget to add new parameters
        self.addParam = AddParameterWidget(self)
        self.addParam.newParamRequested.connect(self.addParameter)
        self.parameterCreationError.connect(self.addParam.setError)
        self.parameterCreated.connect(self.addParam.clear)
        layout.addWidget(self.addParam)

        self.setLayout(layout)
        self.populateList()

        # creating subscriber client and initializing it
        self.thread = QtCore.QThread()
        self.updateClient = SubClient([self._instrument.name])
        self.updateClient.moveToThread(self.thread)

        # connecting starting slot with the main loop of the subscriber client
        self.thread.started.connect(self.updateClient.connect)
        self.updateClient.update.connect(self.refreshParameter)

        # starts the updateClient in a separate thread. The
        self.thread.start()

    def _makeToolbar(self):
        toolbar = QtWidgets.QToolBar(self)
        toolbar.setIconSize(QtCore.QSize(16, 16))

        refreshAction = toolbar.addAction(
            QtGui.QIcon(":/icons/refresh.svg"),
            "refresh all parameters from the instrument",
        )
        refreshAction.triggered.connect(lambda x: self.refreshAll())

        loadParamAction = toolbar.addAction(
            QtGui.QIcon(":/icons/load.svg"),
            "Load parameters from file",
        )
        loadParamAction.triggered.connect(lambda x: self.loadFromFile())

        saveParamAction = toolbar.addAction(
            QtGui.QIcon(":/icons/save.svg"),
            "Save parameters to file",
        )
        saveParamAction.triggered.connect(lambda x: self.saveToFile())

        toolbar.addSeparator()

        expandAction = toolbar.addAction(
            QtGui.QIcon(":/icons/expand.svg"),
            "expand the parameter tree",
        )
        expandAction.triggered.connect(lambda x: self.plist.expandAll())

        collapseAction = toolbar.addAction(
            QtGui.QIcon(":/icons/collapse.svg"),
            "collapse the parameter tree",
        )
        collapseAction.triggered.connect(lambda x: self.plist.collapseAll())

        toolbar.addSeparator()

        # Debugging tools keep commented for commits.
        # printAction = toolbar.addAction(
        #     QtGui.QIcon(":/icons/code.svg"),
        #     "print empty space",
        # )
        # printAction.triggered.connect(lambda x: print('\n \n \n \n \n'))
        #
        # toolbar.addSeparator()

        self.filterEdit = QtWidgets.QLineEdit(self)
        self.filterEdit.textChanged.connect(self.filterParameters)
        toolbar.addWidget(QtWidgets.QLabel('Filter:'))
        toolbar.addWidget(self.filterEdit)

        return toolbar

    def getParameter(self, fullName: str):
        param = nestedAttributeFromString(self._instrument, fullName)
        return param

    def populateList(self):
        for pname in sorted(toParamDict([self._instrument])):
            fullName = '.'.join(pname.split('.')[1:])
            param = self.getParameter(fullName)
            self.addParameterWidget(fullName, param)

        self.plist.expandAll()
        self.plist.resizeColumnToContents(0)
        self.plist.resizeColumnToContents(1)

    def addParameter(self, fullName: str, value: Any, unit: str,
                     parameterType: ParameterTypes,
                     valsArgs: Optional[str] = '') -> None:
        """Add a new parameter to the instrument.

        :param fullName: parameter name, incl. submodules, excl. instrument name.
        :param value: the value of the parameter, as string.
            if the parameter type is not string, must be possible to evaluate to
            the right type.
        :param unit: physical unit of the parameter
        :param parameterType: determines the validator we will use.
            see: :class:`.params.ParameterType`.
        :param valsArgs: a string that will be converted as args and kwargs for
            creation of the validator. see :func:`.helpers.stringToArgsAndKwargs`.
        :return: None
        """
        try:
            args, kw = stringToArgsAndKwargs(valsArgs)
        except ValueError as e:
            self.parameterCreationError.emit(f'Cannot create parameter. Validator'
                                             f'arguments invalid (how ironic):'
                                             f'{e.args}')
            return
        vals = parameterTypes[parameterType]['validatorType'](*args, **kw)

        if parameterType is not ParameterTypes.string:
            try:
                value = eval(value)
            except Exception as e:
                value = str(value)

        # Checking that the new parameter is not an existing submodule.
        fullName_submodules = fullName.split('.')
        fullName_length = len(fullName_submodules)

        equal = False

        # we go through all of the parameters to see if the new parameter has the same name as an existing submodule
        for param in self._instrument.list():
            param_submodules = param.split('.')
            param_length = len(param_submodules)
            top_level = param_submodules[0]

            # we check if either the new parameter or the existing parameter has
            # more modules and use the smaller one to construct the top-level submodules
            if fullName_length > param_length:
                assembly_number = param_length

            # this only happens when the new parameter has the same name as an existing parameter and not a submodule
            # only used to display correct  error
            elif fullName_length == param_length:
                assembly_number = fullName_length
                equal = True

            else:
                assembly_number = fullName_length

            # construct the top level submodule
            for i in range(1, assembly_number):
                top_level = top_level + '.' + param_submodules[i]

            if fullName == top_level:
                if equal:
                    self.parameterCreationError.emit(f"Could not create parameter. {fullName} "
                                                     f"is an existing parameter.")

                    return
                else:
                    self.parameterCreationError.emit(f"Could not create parameter. {fullName} "
                                                     f"is an existing submodule.")
                    return

        try:
            # Validators are commented out until they can be serialized.
            self._instrument.add_parameter(fullName, initial_value=value,
                                           unit=unit,) # vals=vals)
        except Exception as e:
            self.parameterCreationError.emit(f"Could not create parameter."
                                             f"Adding parameter raised"
                                             f"{type(e)}: {e.args}")
            return

        # we cannot get the real instrument .parameter() function because we
        # want the proxy parameter, of course.
        param = self.getParameter(fullName)
        self.addParameterWidget(fullName, param)
        self.parameterCreated.emit()

    def addParameterWidget(self, fullName: str, parameter: Parameter):
        item = self.plist.addParameter(parameter, fullName)

        rw = self.makeRemoveWidget(fullName)
        self._removeWidgets[fullName] = rw

        w = ParameterWidget(parameter, parent=self, additionalWidgets=[rw])
        self._widgets[fullName] = w
        self.plist.setItemWidget(item, 2, w)

    def makeRemoveWidget(self, fullName: str):
        w = QtWidgets.QPushButton(
            QtGui.QIcon(":/icons/delete.svg"), "", parent=self)
        w.setStyleSheet("""
            QPushButton { background-color: salmon }
        """)
        w.setToolTip("Delete this parameter")
        keepSmallHorizontally(w)

        w.pressed.connect(lambda: self.removeParameter(fullName))
        return w

    def removeParameter(self, fullName: str, deleteServerSide: Optional[bool] = True):
        items = self.plist.findItems(
            fullName, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)
        if len(items) > 0:
            item = items[0]
            parent = item.parent()
            if isinstance(parent, QtWidgets.QTreeWidgetItem):
                parent.removeChild(item)
            else:
                self.plist.takeTopLevelItem(self.plist.indexOfTopLevelItem(item))
            del item

        if fullName in self.plist.parameters:
            self.plist.parameters.remove(fullName)

        if fullName in self._widgets:
            self._widgets[fullName].deleteLater()
            del self._widgets[fullName]

        if fullName in self._removeWidgets:
            self._removeWidgets[fullName].deleteLater()
            del self._removeWidgets[fullName]
        if deleteServerSide:
            if self._instrument.has_param(fullName):
                self._instrument.remove_parameter(fullName)

        self.plist.removeEmptyContainers()

    def refreshAll(self, delete: Optional[bool] = True, unitCheck: Optional[bool] = False):
        """Refreshes the state of the GUI.

        :param delete: Optional, If False, it will not delete parameters when it updates.
        :param unitCheck:  If true, the refresh will check for changes in units, not only values

        Encapsulated in a separate object so we can run it in a separate thread.
        """

        # first, we need to make sure we update the proxy instrument (if using one)
        if isinstance(self._instrument, ProxyInstrument):
            self._instrument.update()

        # next, we can parse through the parameters and update the GUI.
        insParams = self._instrument.list()
        for n in insParams:
            items = self.plist.findItems(n, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)
            if len(items) == 0:
                self.addParameterWidget(n, self.getParameter(n))
            else:
                # this is grabbing the widget consisting of input and all the buttons
                w = self.plist.itemWidget(items[0], 2)
                w.setWidgetFromParameter()

                # also need to check the unit (doesn't happen often, but can!)
                if unitCheck:
                    newUnit = self.getParameter(n).unit
                    items[0].setText(1, newUnit)

        if delete:
            deleteParams = []
            for pn in self.plist.parameters:
                if pn not in insParams:
                    deleteParams.append(pn)
            for pn in deleteParams:
                self.removeParameter(pn)

    def filterParameters(self, filterString: str):
        self.plist.filterItems(filterString)

    def saveToFile(self):
        try:
            self._instrument.toFile()
        except Exception as e:
            logger.info(f"Saving failed. {type(e)}: {e.args}")

    def loadFromFile(self):
        try:
            self._instrument.fromFile(deleteMissing=False)
            self.refreshAll(delete=False, unitCheck=True)

        except Exception as e:
            logger.info(f"Loading failed. {type(e)}: {e.args}")

    @QtCore.Slot(ParameterBroadcastBluePrint)
    def refreshParameter(self, bp: ParameterBroadcastBluePrint):
        """
        Refreshes the GUI to show real time updates of parameters.
        """
        # getting the full name of the parameter and splitting it
        named_submodules = bp.name.split('.')
        name = named_submodules[1]

        # if a new parameter has been created, refresh all the parameters
        if bp.action == 'parameter-creation':
            self.refreshAll()

        elif bp.action == 'parameter-deletion':
            # if a parameter is being deleted, need to adjust name creation for the end 'remove_parameter' tag
            for i in range(2, len(named_submodules)):
                name = name + '.' + named_submodules[i]

            if name in self.plist.parameters:
                self.removeParameter(name, False)

        # updates the changed parameter
        elif bp.action == 'parameter-update' or bp.action == 'parameter-call':
            # generates the name of the parameter as a string without the instrument name in it
            for i in range(2, len(named_submodules)):
                name = name + '.' + named_submodules[i]
            item = self.plist.findItems(name, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)
            # if the parameter does not exist refresh all the parameters
            if len(item) == 0:
                self.refreshAll()
            else:
                # get the corresponding itemwidget and update the value
                w = self.plist.itemWidget(item[0], 2)
                w.paramWidget.setValue(bp.value)


def _addChildTo(parent, child):
    if isinstance(parent, QtWidgets.QTreeWidget):
        parent.addTopLevelItem(child)
    else:
        parent.addChild(child)


class ParameterList(QtWidgets.QTreeWidget):

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)

        self.setColumnCount(2)
        self.setHeaderLabels(['Parameter name', 'Unit', ''])
        self.setHeaderHidden(False)
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)

        self.parameters = []
        self.filterString = ''

    def addParameter(self, p: Parameter, fullName: str) \
            -> QtWidgets.QTreeWidgetItem:

        path = fullName.split('.')[:-1]
        paramName = fullName.split('.')[-1]

        parent = self
        smName = None
        for sm in path:
            if smName is None:
                smName = sm
            else:
                smName = smName + f".{sm}"

            items = self.findItems(
                smName, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0
            )
            if len(items) == 0:
                newItem = QtWidgets.QTreeWidgetItem([smName, '', ''])
                _addChildTo(parent, newItem)
                parent = newItem
            else:
                parent = items[0]

        paramItem = QtWidgets.QTreeWidgetItem([fullName, f"{p.unit}", ''])
        _addChildTo(parent, paramItem)
        self.parameters.append(fullName)
        self.filterItems(self.filterString)
        return paramItem

    def removeEmptyContainers(self):
        """Delete all items that are not parameters and don't contain any
        parameters."""

        def check(parent):
            parent: Union[QtWidgets.QTreeWidgetItem, ParameterList]
            removeList = []

            nChildren = parent.childCount() if \
                isinstance(parent, QtWidgets.QTreeWidgetItem) else \
                parent.topLevelItemCount()

            for i in range(nChildren):
                if isinstance(parent, QtWidgets.QTreeWidgetItem):
                    item = parent.child(i)
                else:
                    item = parent.topLevelItem(i)

                if item.text(0) not in self.parameters:
                    check(item)
                    nGrandChildren = item.childCount()
                    if nGrandChildren == 0:
                        removeList.append(item)

            for item in removeList:
                if isinstance(parent, QtWidgets.QTreeWidgetItem):
                    parent.removeChild(item)
                else:
                    parent.takeTopLevelItem(parent.indexOfTopLevelItem(item))

        check(self)

    def filterItems(self, filterString: str):
        self.filterString = filterString.strip()
        for pn in self.parameters:
            self.showItem(pn, self.filterString in pn)

        def hideEmptyParent(parent):
            hideme = True
            nChildren = parent.childCount() if \
                isinstance(parent, QtWidgets.QTreeWidgetItem) else \
                parent.topLevelItemCount()

            for i in range(nChildren):
                if isinstance(parent, QtWidgets.QTreeWidgetItem):
                    item = parent.child(i)
                else:
                    item = parent.topLevelItem(i)
                if item.text(0) not in self.parameters:
                    hideEmptyParent(item)
                if not item.isHidden():
                    hideme = False

            if isinstance(parent, QtWidgets.QTreeWidgetItem):
                parent.setHidden(hideme)

        hideEmptyParent(self)

    def showItem(self, name: str, show: bool):
        item = self.findItems(
            name, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)[0]
        item.setHidden(not show)


class AddParameterWidget(QtWidgets.QWidget):
    """A widget that allows parameter creation.

    :param parent: parent widget
    :param typeInput: if ``True``, add input fields for creating a value
        validator.
    """

    #: Signal(str, str, str, ParameterTypes, str)
    newParamRequested = QtCore.Signal(str, str, str, ParameterTypes, str)

    #: Signal(str)
    invalidParamRequested = QtCore.Signal(str)

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None,
                 typeInput: bool = False):
        super().__init__(parent)

        self.typeInput = typeInput

        layout = QtWidgets.QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.nameEdit = QtWidgets.QLineEdit(self)
        lbl = QtWidgets.QLabel("Name:")
        lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        layout.addWidget(lbl, 0, 0)
        layout.addWidget(self.nameEdit, 0, 1)

        self.valueEdit = QtWidgets.QLineEdit(self)
        lbl = QtWidgets.QLabel("Value:")
        lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        layout.addWidget(lbl, 0, 2)
        layout.addWidget(self.valueEdit, 0, 3)

        self.unitEdit = QtWidgets.QLineEdit(self)
        lbl = QtWidgets.QLabel("Unit:")
        lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        layout.addWidget(lbl, 0, 4)
        layout.addWidget(self.unitEdit, 0, 5)

        if typeInput:
            self.typeSelect = QtWidgets.QComboBox(self)
            names = []
            for t, v in parameterTypes.items():
                names.append(v['name'])
            for n in sorted(names):
                self.typeSelect.addItem(n)
            self.typeSelect.setCurrentText(parameterTypes[ParameterTypes.numeric]['name'])
            lbl = QtWidgets.QLabel("Type:")
            lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            layout.addWidget(lbl, 1, 0)
            layout.addWidget(self.typeSelect, 1, 1)

            self.valsArgsEdit = QtWidgets.QLineEdit(self)
            lbl = QtWidgets.QLabel('Type opts.:')
            lbl.setToolTip("Optional, for constraining parameter values."
                           "Allowed args and defaults:\n"
                           " - 'Numeric': min_value=-1e18, max_value=1e18\n"
                           " - 'Integer': min_value=-inf, max_value=inf\n"
                           " - 'String': min_length=0, max_length=1e9\n"
                           'See qcodes.utils.validators for details.')
            lbl.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            layout.addWidget(lbl, 1, 2)
            layout.addWidget(self.valsArgsEdit, 1, 3)

        self.addButton = QtWidgets.QPushButton(
            QtGui.QIcon(":/icons/plus-square.svg"),
            ' Add',
            parent=self)

        self.addButton.clicked.connect(self.requestNewParameter)
        self.nameEdit.returnPressed.connect(self.addButton.click)
        self.valueEdit.returnPressed.connect(self.addButton.click)
        self.unitEdit.returnPressed.connect(self.addButton.click)
        layout.addWidget(self.addButton, 0, 6, 1, 1)

        self.clearButton = QtWidgets.QPushButton(
            QtGui.QIcon(":/icons/delete.svg"),
            ' Clear',
            parent=self)

        self.clearButton.setAutoDefault(True)
        self.clearButton.clicked.connect(self.clear)
        layout.addWidget(self.clearButton, 0, 7, 1, 1)

        self.setLayout(layout)
        self.invalidParamRequested.connect(self.setError)

    @QtCore.Slot()
    def clear(self):
        self.clearError()
        self.nameEdit.setText('')
        self.valueEdit.setText('')
        self.unitEdit.setText('')
        if self.typeInput:
            self.typeSelect.setCurrentText(parameterTypes[ParameterTypes.numeric]['name'])
            self.valsArgsEdit.setText('')

    @QtCore.Slot(bool)
    def requestNewParameter(self, _):
        self.clearError()

        name = self.nameEdit.text().strip()
        if len(name) == 0:
            self.invalidParamRequested.emit("Name must not be empty.")
            return
        value = self.valueEdit.text()
        unit = self.unitEdit.text()

        if hasattr(self, 'typeSelect'):
            ptype = paramTypeFromName(self.typeSelect.currentText())
            valsArgs = self.valsArgsEdit.text()
        else:
            ptype = ParameterTypes.any
            valsArgs = ''

        self.newParamRequested.emit(name, value, unit, ptype, valsArgs)

    @QtCore.Slot(str)
    def setError(self, message: str):
        self.addButton.setStyleSheet("""
        QPushButton { background-color: red }
        """)
        self.addButton.setToolTip(message)

    def clearError(self):
        self.addButton.setStyleSheet("")
        self.addButton.setToolTip("")


class InstrumentParameters(QtWidgets.QWidget):
    """
    Widget that displays all the parameters of an instrument. It updates live with its values through the server broadcasts.
    You can modify the values of parameters directly from the widget.
    """
    def __init__(self, ins, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ins = ins

        self._layout = QtWidgets.QVBoxLayout(self)
        self.setLayout(self._layout)

        self.toolbar, self.filterEdit = _makeToolbar(self)
        self._layout.addWidget(self.toolbar)

        self.plist = ParameterList(self)
        self._layout.addWidget(self.plist)

        self.collapseAction = QtWidgets.QAction('Collapse all')
        self.expandAction = QtWidgets.QAction(f'Expand all')

        self.collapseAction.triggered.connect(self.plist.collapseAll)
        self.expandAction.triggered.connect(self.plist.expandAll)

        self.conextMenu = QtWidgets.QMenu(self)
        self.conextMenu.addAction(self.collapseAction)
        self.conextMenu.addAction(self.expandAction)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda x: self.conextMenu.exec_(self.mapToGlobal(x)))

        self.widgets = {}

        for paramName, param in self.ins.parameters.items():
            self.addParameterWidget(paramName, param)

        self.addSubmodules()
        self.plist.expandAll()

        self.cliThread = QtCore.QThread()
        self.subClient = SubClient([self.ins.name])
        self.subClient.moveToThread(self.cliThread)

        self.cliThread.started.connect(self.subClient.connect)
        self.subClient.update.connect(self.updateParameter)

        self.cliThread.start()

    @QtCore.Slot(ParameterBroadcastBluePrint)
    def updateParameter(self, bp: ParameterBroadcastBluePrint):

        def _addParameter(_bp):
            paramName = '.'.join(_bp.name.split('.')[1:])
            # The parameter might not be in the proxy instrument yet, so you need to update it.
            if paramName not in self.ins.list():
                self.ins.update()
            if paramName in self.ins.list():
                self.addParameterWidget(paramName, nestedAttributeFromString(self.ins, paramName))

        # TODO: revise what this actually means
        # getting the full name of the parameter and splitting it
        named_submodules = bp.name.split('.')
        name = named_submodules[1]

        # if a new parameter has been created, refresh all the parameters
        if bp.action == 'parameter-creation':
            _addParameter(bp)

        elif bp.action == 'parameter-deletion':
            # if a parameter is being deleted, need to adjust name creation for the end 'remove_parameter' tag
            for i in range(2, len(named_submodules)):
                name = name + '.' + named_submodules[i]

            if name in self.plist.parameters:
                self.removeParameter(name)

        # updates the changed parameter
        elif bp.action == 'parameter-update' or bp.action == 'parameter-call':
            # generates the name of the parameter as a string without the instrument name in it
            for i in range(2, len(named_submodules)):
                name = name + '.' + named_submodules[i]
            item = self.plist.findItems(name, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)
            # if the parameter does not exist refresh all the parameters
            if len(item) == 0:
                _addParameter(bp)
            else:
                # get the corresponding itemwidget and update the value
                w = self.plist.itemWidget(item[0], 2)
                w.paramWidget.setValue(bp.value)

    def addSubmodules(self, submod = None, prefix=None):
        if submod is None and prefix is None:
            if hasattr(self.ins, 'instrument_modules'):
                for submodName, _submod in self.ins.instrument_modules.items():
                    self.addSubmodules(_submod, submodName)
                return

        else:
            if hasattr(submod, 'instrument_modules'):
                for submodName, _submod in submod.instrument_modules.items():
                    self.addSubmodules(_submod, prefix + '.' + submodName)

        for paramName, param in submod.parameters.items():
            self.addParameterWidget(prefix + '.' + paramName, param)

    def addParameterWidget(self, fullName: str, parameter: Parameter):
        item = self.plist.addParameter(parameter, fullName)
        widget = ParameterWidget(parameter, parent=self)
        self.widgets[fullName] = widget
        self.plist.setItemWidget(item, 2, widget)

    def removeParameter(self, fullName: str):
        items = self.plist.findItems(fullName, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)
        if len(items) > 0:
            item = items[0]
            parent = item.parent()
            if parent is None:
                self.plist.takeTopLevelItem(self.plist.indexOfTopLevelItem(item))
                del item
            else:
                parent.removeChild(item)
                del item
                if parent.childCount() == 0:
                    self.removeParameter(parent.text(0))

        if fullName in self.plist.parameters:
            self.plist.parameters.remove(fullName)

        if fullName in self.widgets:
            self.widgets[fullName].deleteLater()
            del self.widgets[fullName]

    @QtCore.Slot(str)
    def filterItems(self, filterString: str):
        self.plist.filterItems(filterString)

    @QtCore.Slot()
    def refreshAll(self):
        print(f'refresh all has been pressed PARAMETERS')

    @QtCore.Slot()
    def expandAll(self):
        self.plist.expandAll()

    @QtCore.Slot()
    def collapseAll(self):
        self.plist.collapseAll()

    @QtCore.Slot()
    def promoteStar(self):
        print(f'In the methods the promote star has been called')

    @QtCore.Slot()
    def hideTrash(self):
        print(f'In the methods hide trash has been called')

    def closeEvent(self, event):
        self.cliThread.quit()
        self.cliThread.deleteLater()
        self.cliThread = None
        super().closeEvent(event)


def getTooltipFromFun(fun: Callable):
    """
    Returns the signature of the function with its documentation underneath.
    """
    sig = inspect.signature(fun)
    doc = inspect.getdoc(fun)
    return str(sig) + '\n\n' + str(doc)


class AnyInputForMethod(AnyInput):
    """
    Implementation of AnyInput that can process arguments and keyword arguments to use for methods.
    You can add multiple arguments if they are separated by a comma. If the '=' is present in any argument, it will
    be treated like a keyword argument with the string in front of the equal sign as the key, and the evaluated value.

    All arguments and keyword arguments are evaluated if the doEval button is checked, if not everything is treated like
    a long string.
    """
    def value(self):
        if self.doEval.isChecked():
            # If '=' is present we need to separate the keyword from the value
            # If ',' is present we have more than one argument.
            if '=' in self.input.text() or ',' in self.input.text():
                rawArgs = self.input.text().split(',')
                args = []
                kwargs = {}
                for x in rawArgs:
                    if '=' in x:
                        key, value = x.split('=')
                        key = key.replace(" ", "")
                        kwargs[key] = eval(value)
                    else:
                        args.append(eval(x))
                return tuple(args), kwargs
            else:
                return super().value(), None

        return self.input.text(), None


class MethodDisplay(QtWidgets.QWidget):

    #: Signal(str)
    #: emitted when the widget runs a function and fails. Emits the exception as a string.
    runFailed = QtCore.Signal(str)

    #: Signal(str)
    #: emitted when the widget runs a function and is successful. Emits the return value as a string.
    runSuccessful = QtCore.Signal(str)

    def __init__(self, fun, fullName=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fun = fun

        # Only used for logging purposes.
        self.fullName = fullName

        self.anyInput = AnyInputForMethod()
        self.anyInput.input.setPlaceholderText(str(inspect.signature(fun)))
        self.anyInput.input.setToolTip(getTooltipFromFun(fun))
        self.anyInput.input.returnPressed.connect(self.runFun)

        self.runButton = QtWidgets.QPushButton("Run", parent=self)
        self.runButton.clicked.connect(self.runFun)

        self.alertLabel = AlertLabelGreen(parent=self)
        self.runFailed.connect(self.alertLabel.setAlert)
        self.runSuccessful.connect(self.alertLabel.setSuccssefulAlert)

        self._layout = QtWidgets.QHBoxLayout(self)
        self.setLayout(self._layout)
        self._layout.addWidget(self.anyInput)
        self._layout.addWidget(self.runButton)
        self._layout.addWidget(self.alertLabel)

        self._layout.setContentsMargins(1, 1, 1, 1)

    @QtCore.Slot()
    def runFun(self):
        try:
            args, kwargs = self.anyInput.value()
            if kwargs is not None:
                ret = self.fun(*args, **kwargs)
            else:
                if isinstance(args, list) or isinstance(args, tuple):
                    ret = self.fun(*args)
                else:
                    ret = self.fun(args)
            self.runSuccessful.emit(str(ret))
            logger.info(f"'{self.fullName}' returned: {ret}")

        except Exception as e:
            self.runFailed.emit(str(e))
            logger.warning(f"'{self.fullName}' Raised the following execution: {e}")


class MethodsList(QtWidgets.QTreeWidget):
    """
    Widget that will display all the methods of an instrument
    """
    def __init__(self, ins, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ins = ins
        self.methods: Dict[str, MethodDisplay] = {}
        self.filterString = ''

        self.setColumnCount(2)
        self.setHeaderLabels(['Method Name', 'Arguments & Run'])
        self.setHeaderHidden(False)
        self.setSortingEnabled(True)
        self.setAlternatingRowColors(True)

        self.collapseAction = QtWidgets.QAction('Collapse all')
        self.expandAction = QtWidgets.QAction('Expand all')
        self.clearAleartsAction = QtWidgets.QAction('Clear alerts')

        self.collapseAction.triggered.connect(self.collapseAll)
        self.expandAction.triggered.connect(self.expandAll)

        self.conextMenu = QtWidgets.QMenu(self)
        self.conextMenu.addAction(self.clearAleartsAction)
        self.conextMenu.addSeparator()
        self.conextMenu.addAction(self.collapseAction)
        self.conextMenu.addAction(self.expandAction)
        self.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda x: self.conextMenu.exec_(self.mapToGlobal(x)))

        for name, meth in self.ins.functions.items():
            self.addMethod(meth, name)

        self.addSubmodules()
        self.expandAll()

    def addMethod(self, meth, fullName):
        path = fullName.split('.')[:-1]
        paramName = fullName.split('.')[-1]

        parent = self
        smName = None
        for sm in path:
            if smName is None:
                smName = sm
            else:
                smName = smName + f".{sm}"

            items = self.findItems(smName, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)

            if len(items) == 0:
                newItem = QtWidgets.QTreeWidgetItem([smName, ''])
                _addChildTo(parent, newItem)
                parent = newItem
            else:
                parent = items[0]

        methodItem = QtWidgets.QTreeWidgetItem([fullName, ''])
        methodItem.setToolTip(0, getTooltipFromFun(meth))

        _addChildTo(parent, methodItem)
        self.methods[fullName] = meth
        itemWidget = MethodDisplay(meth, self.ins.name + '.' + fullName)
        self.clearAleartsAction.triggered.connect(itemWidget.alertLabel.clearAlert)
        self.setItemWidget(methodItem, 1, itemWidget)
        return methodItem

    def addSubmodules(self, submod=None, prefix=None):
        if submod is None and prefix is None:
            if hasattr(self.ins, 'instrument_modules'):
                for submodName, _submod in self.ins.instrument_modules.items():
                    self.addSubmodules(_submod, submodName)
                return

        else:
            if hasattr(submod, 'instrument_modules'):
                for submodName, _submod in submod.instrument_modules.items():
                    self.addSubmodules(_submod, prefix + '.' + submodName)

        for funName, fun in submod.functions.items():
            self.addMethod(fun, prefix + '.' + funName)

    def filterItems(self, filterString: str):
        self.filterString = filterString.strip()
        for pn in self.methods.keys():
            self.showItem(pn, self.filterString in pn)

        def hideEmptyParent(parent):
            hideme = True
            nChildren = parent.childCount() if \
                isinstance(parent, QtWidgets.QTreeWidgetItem) else \
                parent.topLevelItemCount()

            for i in range(nChildren):
                if isinstance(parent, QtWidgets.QTreeWidgetItem):
                    item = parent.child(i)
                else:
                    item = parent.topLevelItem(i)
                if item.text(0) not in self.methods:
                    hideEmptyParent(item)
                if not item.isHidden():
                    hideme = False

            if isinstance(parent, QtWidgets.QTreeWidgetItem):
                parent.setHidden(hideme)

        hideEmptyParent(self)

    def showItem(self, name: str, show: bool):
        item = self.findItems(
            name, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)[0]
        item.setHidden(not show)

    def sizeHint(self):
        ret = super().sizeHint()
        print(f'The sizeHint for the methods: {ret}')
        return ret


class InstrumentMethods(QtWidgets.QWidget):

    def __init__(self, ins: Union[ProxyInstrument, Instrument], *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ins = ins

        self._layout = QtWidgets.QVBoxLayout(self)
        self.setLayout(self._layout)

        self.toolbar, self.filterEdit = _makeToolbar(self)
        self._layout.addWidget(self.toolbar)

        self.mlist = MethodsList(ins, parent=self)
        self._layout.addWidget(self.mlist)

    @QtCore.Slot(str)
    def filterItems(self, filterString: str):
        self.mlist.filterItems(filterString)

    @QtCore.Slot()
    def refreshAll(self):
        print(f'refresh all has been pressed')

    @QtCore.Slot()
    def expandAll(self):
        self.mlist.expandAll()

    @QtCore.Slot()
    def collapseAll(self):
        self.mlist.collapseAll()

    @QtCore.Slot()
    def promoteStar(self):
        print(f'In the methods the promote star has been called')

    @QtCore.Slot()
    def hideTrash(self):
        print(f'In the methods hide trash has been called')


# TOOD: Figure out the tooltip text for all of these guys
def _makeToolbar(widget: QtWidgets.QWidget):
    toolbar = QtWidgets.QToolBar(widget)
    toolbar.setIconSize(QtCore.QSize(16, 16))

    refreshAction = toolbar.addAction(
        QtGui.QIcon(":/icons/refresh.svg"),
        "refresh all the items from the instrument",
    )
    refreshAction.triggered.connect(lambda x: widget.refreshAll())

    toolbar.addSeparator()

    expandAction = toolbar.addAction(
        QtGui.QIcon(":/icons/expand.svg"),
        "expand in the tree",
    )
    expandAction.triggered.connect(lambda x: widget.expandAll())

    collapseAction = toolbar.addAction(
        QtGui.QIcon(":/icons/collapse.svg"),
        "collapse the parameter tree",
    )
    collapseAction.triggered.connect(lambda x: widget.collapseAll())

    toolbar.addSeparator()

    starAction = toolbar.addAction(
        QtGui.QIcon(':/icons/star.svg'),
        "Move Starred items to the top"
    )
    starAction.setCheckable(True)
    starAction.triggered.connect(lambda x: widget.promoteStar())

    trashAction = toolbar.addAction(
        QtGui.QIcon(":/icons/trash.svg"),
        "Hide trashed items"
    )
    trashAction.setCheckable(True)
    trashAction.triggered.connect(lambda x: widget.hideTrash())

    filterEdit = QtWidgets.QLineEdit(widget)
    filterEdit.textChanged.connect(widget.filterItems)
    filterEdit.setPlaceholderText('Filter Items')
    toolbar.addWidget(filterEdit)

    return toolbar, filterEdit


# TODO: Document everything inside the parameters display classes
# ----------------- Parameters Display Classes-----------------------------

class ItemParameters(ItemBase):
    def __init__(self, unit='', **kwargs):
        super().__init__(**kwargs)

        self.unit = unit


class ParameterDelegate(QtWidgets.QStyledItemDelegate):
    """
    """

    def __init__(self, parent=None):
        super().__init__(parent=parent)

        # Stores as key the name of the item and as value the widget that the delegate creates.
        # used to keep a reference to the widget.
        self.parameters: Dict[str, QtWidgets.QWidget] = {}

    def createEditor(self, QWidget, QStyleOptionViewItem, QModelIndex):
        """
        This is the function that is supposed to create the widget. It should return it.
        """
        proxyModel = QModelIndex.model()
        model = proxyModel.sourceModel()
        item = model.itemFromIndex(proxyModel.mapToSource(QModelIndex))
        if item.column != 0:
            parent = item.parent()
            row = item.row()
            if parent is None:
                item = model.item(row, 0)
            else:
                item = parent.child(row, 0)

        extraObj = item.extra_obj

        ret = ParameterWidget(extraObj, QWidget)
        self.parameters[item.name] = ret
        return ret


class ModelParameters(InstrumentModelBase):

    itemNewValue = QtCore.Signal(object, object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.setColumnCount(3)
        self.setHorizontalHeaderLabels([self.attr, 'unit', ''])

        # Live updates items
        self.cliThread = QtCore.QThread()
        self.subClient = SubClient([self.instrument.name])
        self.subClient.moveToThread(self.cliThread)

        self.cliThread.started.connect(self.subClient.connect)
        self.subClient.update.connect(self.updateParameter)

        self.cliThread.start()

    @QtCore.Slot(ParameterBroadcastBluePrint)
    def updateParameter(self, bp: ParameterBroadcastBluePrint):
        fullName = '.'.join(bp.name.split('.')[1:])

        if bp.action == 'parameter-creation':
            if fullName not in self.instrument.list():
                self.instrument.update()
            if fullName in self.instrument.list():
                self.addItem(fullName, extraObj=nestedAttributeFromString(self.instrument, fullName))

        elif bp.action == 'parameter-deletion':
            self.removeItem(fullName)

        elif bp.action == 'parameter-update' or bp.action == 'parameter-call':
            item = self.findItems(fullName, QtCore.Qt.MatchExactly | QtCore.Qt.MatchRecursive, 0)
            if len(item) == 0:
                self.addItem(fullName, extraObj=nestedAttributeFromString(self.instrument, fullName))
            else:
                # The model can't actually modify the widget since it knows nothing about the view itself.
                self.itemNewValue.emit(item[0].name, bp.value)

    def addChildTo(self, parent: QtGui.QStandardItem, item):
        if item is not None:
            # A parameter might not have a unit
            unit = ''
            if item.extra_obj is not None:
                unit = item.extra_obj.unit
            unitItem = QtGui.QStandardItem(unit)
            extraItem = QtGui.QStandardItem()

            if parent == self:
                rowCount = self.rowCount()
                self.setItem(rowCount, 2, extraItem)
                self.setItem(rowCount, 1, unitItem)
                self.setItem(rowCount, 0, item)
            else:
                parent.appendRow([item, unitItem, extraItem])

            self.newItem.emit(item)


class ParametersTreeView(InstrumentTreeViewBase):
    def __init__(self, model, *args, **kwargs):
        super().__init__(model, [2], *args, **kwargs)

        self.delegate = ParameterDelegate(self)

        self.setItemDelegateForColumn(2, self.delegate)
        self.setAllDelegatesPersistent()

    @QtCore.Slot(object, object)
    def onItemNewValue(self, item, value):
        widget = self.delegate.parameters[item]
        widget.paramWidget.setValue(value)


class NewInstrumentParameters(InstrumentDisplayBase):
    def __init__(self, instrument, *args, **kwargs):
        if 'instrument' in kwargs:
            del kwargs['instrument']

        super().__init__(instrument=instrument,
                         attr='parameters',
                         itemClass=ItemParameters,
                         modelType=ModelParameters,
                         viewType=ParametersTreeView,
                         *args, **kwargs)

    def connectSignals(self):
        super().connectSignals()
        self.model.itemNewValue.connect(self.view.onItemNewValue)

# -------------------------------------------------------------------------


class GenericInstrument(QtWidgets.QWidget):
    """
    Widget that allows the display of real time parameters and changing their values.
    """

    def __init__(self, ins: Union[ProxyInstrument, Instrument], *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ins = ins

        self._layout = QtWidgets.QVBoxLayout(self)
        self.setLayout(self._layout)

        self.splitter = QtWidgets.QSplitter(self)
        self.splitter.setOrientation(QtCore.Qt.Vertical)

        # TODO: Rename the NewInstrumentParameters to InstrumentParameters once the old class has been deleted
        self.parametersList = NewInstrumentParameters(instrument=ins)
        self.methodsList = InstrumentMethods(ins)
        self.instrumentNameLabel = QtWidgets.QLabel(f'{self.ins.name} | type: {type(self.ins)}')

        self._layout.addWidget(self.instrumentNameLabel)
        self._layout.addWidget(self.splitter)
        self.splitter.addWidget(self.parametersList)
        self.splitter.addWidget(self.methodsList)
