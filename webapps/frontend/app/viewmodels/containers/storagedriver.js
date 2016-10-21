// Copyright (C) 2016 iNuron NV
//
// This file is part of Open vStorage Open Source Edition (OSE),
// as available from
//
//      http://www.openvstorage.org and
//      http://www.openvstorage.com.
//
// This file is free software; you can redistribute it and/or modify it
// under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
// as published by the Free Software Foundation, in version 3 as it comes
// in the LICENSE.txt file of the Open vStorage OSE distribution.
//
// Open vStorage is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY of any kind.
/*global define */
define([
    'jquery', 'knockout',
    'ovs/api', 'ovs/generic'
], function($, ko, api, generic) {
    "use strict";
    return function(guid) {
        var self = this;

        // Handles
        self.canBeDeletedHandle = undefined;

        // Observables
        self.guid              = ko.observable(guid);
        self.loaded            = ko.observable(false);
        self.loading           = ko.observable(false);
        self.mountpoint        = ko.observable();
        self.name              = ko.observable();
        self.ports             = ko.observableArray([0, 0, 0, 0]);
        self.storageIP         = ko.observable();
        self.storageRouterGuid = ko.observable();
        self.vdiskGuids        = ko.observableArray([]);

        // Functions
        self.fillData = function(data) {
            generic.trySet(self.mountpoint, data, 'mountpoint');
            generic.trySet(self.name, data, 'name');
            generic.trySet(self.ports, data, 'ports');
            generic.trySet(self.storageIP, data, 'storage_ip');
            generic.trySet(self.storageRouterGuid, data, 'storagerouter_guid');
            generic.trySet(self.vdiskGuids, data, 'vdisks_guids');
            self.loaded(true);
            self.loading(false);
        };
        self.load = function() {
            return $.Deferred(function(deferred) {
                self.loading(true);
                api.get('storagedrivers/' + self.guid())
                    .done(function(data) {
                        self.fillData(data);
                        deferred.resolve();
                    })
                    .fail(deferred.reject)
                    .always(function() {
                        self.loading(false);
                    });
            }).promise();
        };
    };
});
