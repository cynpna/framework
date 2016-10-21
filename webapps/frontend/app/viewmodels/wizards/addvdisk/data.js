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
define(['jquery', 'knockout'], function($, ko){
    "use strict";
    var singleton = function() {
        var wizardData = {
            name:           ko.observable(''),
            sizeEntry:      ko.observable(0).extend({ numeric: { min: 1, max: 65536 } }),
            storageRouter:  ko.observable(),
            storageRouters: ko.observableArray([]),
            vPool:          ko.observable(),
            vPools:         ko.observableArray([])
        };

        // Computed
        wizardData.size = ko.computed(function () {
            return wizardData.sizeEntry() * Math.pow(1024, 3);
        });

        wizardData.storageRoutersByVpool = ko.computed(function() {
            var guids = [], result = [];
            $.each(wizardData.storageRouters(), function(index, storageRouter) {
                if (wizardData.vPool() !== undefined &&
                    storageRouter.vPoolGuids().contains(wizardData.vPool().guid())) {
                    result.push(storageRouter);
                    guids.push(storageRouter.guid());
                }
            });
            if (result.length > 0 && wizardData.storageRouter() && !guids.contains(wizardData.storageRouter().guid())) {
                wizardData.storageRouter(result[0]);
            }
            return result;
        });
        return wizardData;
    };
    return singleton();
});
